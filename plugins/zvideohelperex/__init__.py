from datetime import datetime, timedelta
import sqlite3
import json
from app.plugins.zvideohelperex.DoubanHelper import *
from enum import Enum

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.schemas.types import EventType, NotificationType
from app.core.event import eventmanager, Event
from pathlib import Path

from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
import time

# 豆瓣状态
class DoubanStatus(Enum):
    WATCHING = "do"
    DONE = "collect"


class ZvideoHelperEx(_PluginBase):
    # 插件名称
    plugin_name = "极影视豆瓣同步"
    # 插件描述
    plugin_desc = "在极影视和豆瓣间双向同步再看已看信息。"
    # 插件图标
    plugin_icon = "zvideo.png"
    # 插件版本
    plugin_version = "2.1"
    # 插件作者
    plugin_author = "superxyj2021"
    # 作者主页
    author_url = "https://github.com/superxyj2021"
    # 插件配置项ID前缀
    plugin_config_prefix = "zvideohelperex"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _cron = None
    _notify = False
    _onlyonce = False
    _sync_douban_status = False
    _clean_cache = False
    _private = False
    _reverse_sync_douban_status = False
    _douban_helper = None
    _cached_data: dict = {}
    _db_path = ""
    _cookie = ""
    _zvideo_username = ""
    _douban_user = ""
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    _should_stop = False

    #发现有部分电影的ID豆瓣会跳转到新的ID上去，导致同步失败，这里做下映射
    ID_REPLACEMENTS = {
        34951057: 36069854,  #猩球崛起：新世界
        # 34951058: 36069855,  # 可以添加更多替换规则
    }
    logger.info("⏳ 开始同步已看状态2...")
    #豆瓣没有数据或者异常的几部片子ID，这几个没法标记为已看，过滤掉
    EXCLUDED_DOUBAN_IDS = {
        35196946: "三体 第 1 季",
        26920285: "怪物猎人", 
        26933053: "反击 第 6 季"
    }
    
    def init_plugin(self, config: dict = None):
        self._should_stop = False
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._db_path = config.get("db_path")
            self._cookie = config.get("cookie")
            self._sync_douban_status = config.get("sync_douban_status")
            self._clean_cache = config.get("clean_cache")
            self._private = config.get("private")
            self._reverse_sync_douban_status = config.get("reverse_sync_douban_status")
            self._zvideo_username = config.get("zvideo_username")
            self._douban_user = config.get("douban_user")
            self._douban_helper = DoubanHelper(user_cookie=self._cookie)

        # 获取历史数据
        self._cached_data = (
            self.get_data("zvideohelperex")
            if self.get_data("zvideohelperex") is not None
            else dict()
        )
        # 加载模块
        if self._onlyonce:
            if self._clean_cache:
                self._cached_data = {}
                self.save_data("zvideohelperex", self._cached_data)
                self._clean_cache = False
            # 检查数据库路径是否存在
            path = Path(self._db_path)
            if not path.exists():
                logger.error(f"极影视数据库路径不存在: {self._db_path}")
                self._onlyonce = False
                self._clean_cache = False
                self._update_config()
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title=f"【极影视豆瓣同步】",
                        text=f"极影视数据库路径不存在: {self._db_path}",
                    )
                return

            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info(f"极影视豆瓣同步服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self.do_job,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ))
                + timedelta(seconds=3),
                name="极影视豆瓣同步",
            )
            # 关闭一次性开关
            self._onlyonce = False
            self._update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def _update_config(self):
        self.update_config(
            {
                "onlyonce": False,
                "cron": self._cron,
                "enabled": self._enabled,
                "notify": self._notify,
                "db_path": self._db_path,
                "cookie": self._cookie,
                "sync_douban_status": self._sync_douban_status,
                "clean_cache": self._clean_cache,
                "private": self._private,
                "reverse_sync_douban_status": self._reverse_sync_douban_status,
                "zvideo_username": self._zvideo_username,
                "douban_user": self._douban_user,
            }
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [
            {
                "cmd": "/sync_zvideo_to_douban",
                "event": EventType.PluginAction,
                "desc": "同步极影视观影状态到豆瓣",
                "category": "",
                "data": {"action": "sync_zvideo_to_douban"},
            },
            {
                "cmd": "/sync_douban_to_zvideo",
                "event": EventType.PluginAction,
                "desc": "同步豆瓣已看到极影视",
                "category": "",
                "data": {"action": "sync_douban_to_zvideo"},
            },
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event):
        if event:
            event_data = event.event_data
            if event_data:
                if (
                    event_data.get("action") == "sync_zvideo_to_douban"
                    or event_data.get("action") == "sync_douban_to_zvideo"
                ):
                    if event_data.get("action") == "sync_zvideo_to_douban":
                        logger.info("收到命令，开始同步极影视观影状态 ...")
                        self.post_message(
                            channel=event.event_data.get("channel"),
                            title="开始同步极影视观影状态 ...",
                            userid=event.event_data.get("user"),
                        )
                        self.sync_douban_status()
                        if event:
                            self.post_message(
                                channel=event.event_data.get("channel"),
                                title="同步极影视观影状态完成！",
                                userid=event.event_data.get("user"),
                            )
                    elif event_data.get("action") == "sync_douban_to_zvideo":
                        logger.info("收到命令，同步豆瓣已看到极影视 ...")
                        self.post_message(
                            channel=event.event_data.get("channel"),
                            title="开始同步豆瓣已看 ...",
                            userid=event.event_data.get("user"),
                        )
                        self.reverse_sync_douban_status()
                        if event:
                            self.post_message(
                                channel=event.event_data.get("channel"),
                                title="同步豆瓣已看到极影视完成！",
                                userid=event.event_data.get("user"),
                            )

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [
                {
                    "id": "ZvideoHelperEx",
                    "name": "极影视豆瓣同步",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.do_job,
                    "kwargs": {},
                }
            ]

    def do_job(self):
        self._should_stop = False
        if self._reverse_sync_douban_status:
            self.reverse_sync_douban_status()
        if self._sync_douban_status:
            self.sync_douban_status()


    def set_douban_watching(self):
        logger.info("⏳ 开始同步在看状态...")
        watching_douban_id = []
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT collection_id FROM zvideo_playlist")
            collection_ids = cursor.fetchall()
            collection_ids = set([collection_id[0] for collection_id in collection_ids])
            meta_info_list = []
            for collection_id in collection_ids:
                if self._should_stop:
                    logger.info("检测到中断请求，停止同步在看状态...")
                    break
                cursor.execute(
                    "SELECT meta_info FROM zvideo_collection WHERE collection_id = ? AND type = 200",
                    (collection_id,),
                )
                rows = cursor.fetchall()
                for row in rows:
                    if self._should_stop:
                        logger.info("检测到中断请求，停止同步在看状态...")
                        break
                    try:
                        meta_info_json = json.loads(row[0])
                        meta_info_list.append(meta_info_json)
                    except json.JSONDecodeError as e:
                        logger.error(
                            f"An error occurred while decoding JSON for collection_id {collection_id}: {e}"
                        )
            for meta_info in meta_info_list:
                if self._should_stop:
                    logger.info("检测到中断请求，停止同步在看状态...")
                    break
                try:
                    douban_id = meta_info["relation"]["douban"]["douban_id"]
                    title = meta_info["title"]
                except Exception as e:
                    logger.error(f"meta_info: {meta_info}，解析失败: {e}")
                    continue
                if self._cached_data.get(title) is not None:
                    logger.info(f"ℹ️ 已处理过: {title}，跳过...")
                    continue
                if douban_id == 0: #豆瓣ID为0的直接跳过，没必要去查找
                    continue
                if douban_id is not None:
                    watching_douban_id.append((title, douban_id))
                else:
                    logger.error(f"未找到豆瓣ID: {title}")

        except sqlite3.Error as e:
            logger.error(f"An error occurred: {e}")

        finally:
            # 确保游标和连接在使用完后关闭
            if cursor:
                cursor.close()
            if conn:
                conn.close()
            message = ""
            for item in watching_douban_id:
                status = DoubanStatus.WATCHING.value
                ret = self._douban_helper.set_watching_status(
                    subject_id=item[1], status=status, private=self._private
                )
                if ret:
                    self._cached_data[item[0]] = status
                    logger.info(f"✅ title: {item[0]}, douban_id: {item[1]}，已标记为在看")
                    message += f"{item[0]}，已标记为在看\n"
                else:
                    logger.error(
                        f"⚠️ title: {item[0]}, douban_id: {item[1]}，标记在看失败"
                    )
                    message += f"{item[0]}，***标记在看失败***\n"
            if self._notify and len(message) > 0:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【极影视豆瓣同步】",
                    text=message,
                )

    def set_douban_done(self):
        logger.info("⏳ 开始同步已看状态...")
        watching_douban_id = []
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            
            #"""安全地获取收藏ID"""
            excluded_ids = list(self.EXCLUDED_DOUBAN_IDS.keys())
            
            # 基础查询
            sql_parts = [
                "SELECT t.collection_id",
                "FROM zvideo_collection_tags t",
                "JOIN zvideo_collection c ON t.collection_id = c.collection_id",
                "WHERE t.tag_name = '是否看过'",
                "AND c.extend_type != 7"
            ]
            
            params = []
            
            # 处理排除ID
            if excluded_ids:
                placeholders = ','.join(['?' for _ in excluded_ids])
                sql_parts.append(f"AND c.douban_id NOT IN ({placeholders})")
                params.extend(excluded_ids)
            
            # 构建完整SQL
            sql = "\n".join(sql_parts)
            
            cursor.execute(sql, params)
            
            collection_ids = cursor.fetchall()
            collection_ids = set([collection_id[0] for collection_id in collection_ids])
            meta_info_list = []
            for collection_id in collection_ids:
                if self._should_stop:
                    logger.info("检测到中断请求，停止同步已看状态...")
                    break
                cursor.execute(
                    "SELECT meta_info FROM zvideo_collection WHERE collection_id = ?",
                    (collection_id,),
                )
                rows = cursor.fetchall()
                for row in rows:
                    if self._should_stop:
                        logger.info("检测到中断请求，停止同步已看状态...")
                        break
                    try:
                        meta_info_json = json.loads(row[0])
                        meta_info_list.append(meta_info_json)
                    except json.JSONDecodeError as e:
                        logger.error(
                            f"An error occurred while decoding JSON for collection_id {collection_id}: {e}"
                        )
            for meta_info in meta_info_list:
                if self._should_stop:
                    logger.info("检测到中断请求，停止同步已看状态...")
                    break
                try:
                    douban_id = meta_info["relation"]["douban"]["douban_id"]
                    # 使用映射替换
                    douban_id = self.ID_REPLACEMENTS.get(douban_id, douban_id)
                    title = meta_info["title"]
                except Exception as e:
                    logger.error(f"meta_info: {meta_info}，解析失败: {e}")
                    continue
                if self._cached_data.get(title) == DoubanStatus.DONE.value:
                    logger.info(f"ℹ️ 已处理过: {title}，跳过...")
                    continue
                if douban_id == 0: #豆瓣ID为0的直接跳过，没必要去查找
                    continue
                if douban_id is not None:
                    watching_douban_id.append((title, douban_id))
                else:
                    logger.error(f"未找到豆瓣ID: {title}")

        except sqlite3.Error as e:
            logger.error(f"An error occurred: {e}")

        finally:
            # 确保游标和连接在使用完后关闭
            if cursor:
                cursor.close()
            if conn:
                conn.close()
            message = ""
            for item in watching_douban_id:
                status = DoubanStatus.DONE.value
                ret = self._douban_helper.set_watching_status(
                    subject_id=item[1], status=status, private=self._private
                )
                if ret:
                    self._cached_data[item[0]] = status
                    logger.info(f"✅ title: {item[0]}, douban_id: {item[1]},已标记为已看")
                    message += f"{item[0]}，已标记为已看\n"
                else:
                    logger.error(
                        f"⚠️ title: {item[0]}, douban_id: {item[1]}, 标记已看失败"
                    )
                    message += f"{item[0]}，***标记已看失败***\n"
            if self._notify and len(message) > 0:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【极影视豆瓣同步】",
                    text=message,
                )

    def reverse_sync_douban_status(self):
        
        logger.info(f"⏳ 开始同步豆瓣数据到极影视...")
        # 连接到数据库
        conn = sqlite3.connect(self._db_path)
        conn.text_factory = str
        cursor = conn.cursor()
        
        try:
            # 遍历fetch_all_movies返回的所有电影数据
            for movie in self._douban_helper.fetch_all_movies(douban_user=self._douban_user):
                if self._should_stop:
                    logger.info("检测到中断请求，停止同步已看状态...")
                    break
                # 1. 检查status是否为'看过'
                if movie.get('status') != '看过':
                    continue
                
                douban_id = movie.get('douban_id')
                rating_date = movie.get('rating_date')
                
                if not douban_id or not rating_date:
                    logger.info(f"⚠️ 数据不完整: {movie.get('title')}，跳过")
                    continue
                
                logger.info(f"正在处理: {movie.get('title')} (豆瓣ID: {douban_id})")
                
                # 2. 在zvideo_collection中查找是否存在该douban_id的条目
                cursor.execute("""
                    SELECT collection_id 
                    FROM zvideo_collection 
                    WHERE douban_id = ?
                """, (int(douban_id),))
                
                result = cursor.fetchone()
                
                if not result:
                    logger.info(f"ℹ️ 数据库中未找到:{movie.get('title')} (豆瓣ID: {douban_id})，跳过")
                    continue
                
                collection_id = result[0]
                
                # 3. 检查zvideo_collection_tags中是否已存在tag_type=9的条目
                cursor.execute("""
                    SELECT id 
                    FROM zvideo_collection_tags 
                    WHERE collection_id = ? 
                    AND tag_type = 9
                    AND user_name = ?
                """, (collection_id, self._zvideo_username))
                
                existing_tag = cursor.fetchone()
                
                if existing_tag:
                    logger.info(f"ℹ️ 已同步过: {movie.get('title')} (豆瓣ID: {douban_id})，跳过")
                    continue
                
                # 4. 简化时间处理：直接在豆瓣时间后面加上固定字符串
                # 豆瓣格式: "2026-01-05"
                # 目标格式: "2026-01-05 12:00:00.000000000+08:00"
                created_at_str = f"{rating_date} 12:00:00.000000000+08:00"
                logger.info(f"📅 处理时间: {created_at_str}")
                
                # 5. 插入新的标签记录
                cursor.execute("""
                    INSERT INTO zvideo_collection_tags 
                    (user_name, collection_id, tag_id, tag_type, tag_name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    self._zvideo_username,  # 使用用户输入的user_name
                    collection_id,
                    1,      # tag_id固定为1
                    9,      # tag_type固定为9
                    '是否看过',
                    created_at_str
                ))
                
                logger.info(f"✅ 成功同步: {movie.get('title')} (豆瓣ID: {douban_id}) (用户: {self._zvideo_username})")
                
                # 提交当前插入
                conn.commit()
                
        except Exception as e:
            logger.error(f"❌ 处理过程中发生错误: {e}")
            conn.rollback()
            raise
        
        finally:
            # 关闭数据库连接
            conn.close()
        #同步豆瓣到极影视为一次性任务，完成后关闭选项
        self._reverse_sync_douban_status = False
        self._update_config()

    def sync_douban_status(self):
        self.set_douban_watching()
        self.set_douban_done()
        # 缓存数据
        self.save_data("zvideohelperex", self._cached_data)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "开启通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "sync_douban_status",
                                            "label": "单向同步（极影视->豆瓣）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "reverse_sync_douban_status",
                                            "label": "双向同步（豆瓣-极影视）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clean_cache",
                                            "label": "清理缓存数据",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "private",
                                            "label": "豆瓣状态仅自己可见",
                                        },
                                    }
                                ],
                            },                            
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "cron", "label": "执行周期"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "zvideo_username",
                                            "label": "极影视用户名",
                                            "placeholder": "填入极空间用户名。",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "douban_user",
                                            "label": "豆瓣ID",
                                            "placeholder": "在豆瓣APP或者网页中，我的-头像附近就能看到。",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "cookie",
                                            "label": "豆瓣cookie",
                                            "rows": 1,
                                            "placeholder": "留空则从cookiecloud获取",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "db_path",
                                            "label": "极影视数据库路径",
                                            "rows": 1,
                                            "placeholder": "极影视数据库路径为/zspace/zsrp/sqlite/zvideo/zvideo.db，需先映射路径",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "error",
                                            "variant": "tonal",
                                            "text": "强烈建议使用前备份数据库，以免因插件bug导致数据库异常",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "本插件基于极影视数据库扩展功能，需开启SSH后通过Portainer等工具映射极影视数据库路径",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "双向同步仅执行一次，执行后会自动关闭。该选项会先将豆瓣已看数据同步到极影视中。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "极空间用户名，用于同步豆瓣已看至极影视。极影视数据库的观看状态需要绑定用户名。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "cron": "0 0 * * *",
            "douban_score_update_days": 0,
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        self._should_stop = True
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))
