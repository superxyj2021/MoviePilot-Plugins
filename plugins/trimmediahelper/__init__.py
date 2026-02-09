from datetime import datetime, timedelta
import sqlite3
import json
from app.plugins.trimmediahelper.DoubanHelper import *
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

class TrimMediaHelper(_PluginBase):
    # 插件名称
    plugin_name = "飞牛影视豆瓣同步"
    # 插件描述
    plugin_desc = "在飞牛影视和豆瓣间双向同步再看已看信息。"
    # 插件图标
    plugin_icon = "zvideo.png"
    # 插件版本
    plugin_version = "2.0"
    # 插件作者
    plugin_author = "superxyj2021"
    # 作者主页
    author_url = "https://github.com/superxyj2021"
    # 插件配置项ID前缀
    plugin_config_prefix = "trimmediahelper"
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
    _trimmedia_user = ""
    _douban_user = ""
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    _should_stop = False

   
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
            self._trimmedia_user = config.get("trimmedia_user")
            self._douban_user = config.get("douban_user")
            self._douban_helper = DoubanHelper(user_cookie=self._cookie)

        # 获取历史数据
        self._cached_data = (
            self.get_data("trimmediahelper")
            if self.get_data("trimmediahelper") is not None
            else dict()
        )
        # 加载模块
        if self._onlyonce:
            if self._clean_cache:
                self._cached_data = {}
                self.save_data("trimmediahelper", self._cached_data)
                self._clean_cache = False
            # 检查数据库路径是否存在
            path = Path(self._db_path)
            if not path.exists():
                logger.error(f"飞牛影视数据库路径不存在: {self._db_path}")
                self._onlyonce = False
                self._clean_cache = False
                self._update_config()
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title=f"【=飞牛影视豆瓣同步】",
                        text=f"飞牛影视数据库路径不存在: {self._db_path}",
                    )
                return

            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info(f"飞牛影视豆瓣同步服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self.do_job,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ))
                + timedelta(seconds=3),
                name="飞牛影视豆瓣同步",
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
                "trimmedia_user": self._trimmedia_user,
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
                "cmd": "/sync_trimmedia_to_douban",
                "event": EventType.PluginAction,
                "desc": "同步飞牛影视观影状态到豆瓣",
                "category": "",
                "data": {"action": "sync_trimmedia_to_douban"},
            },
            {
                "cmd": "/sync_douban_to_trimmedia",
                "event": EventType.PluginAction,
                "desc": "同步豆瓣已看到飞牛影视",
                "category": "",
                "data": {"action": "sync_douban_to_trimmedia"},
            },
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event):
        if event:
            event_data = event.event_data
            if event_data:
                if (
                    event_data.get("action") == "sync_trimmedia_to_douban"
                    or event_data.get("action") == "sync_douban_to_trimmedia"
                ):
                    if event_data.get("action") == "sync_trimmedia_to_douban":
                        logger.info("收到命令，开始同步飞牛影视观影状态 ...")
                        self.post_message(
                            channel=event.event_data.get("channel"),
                            title="开始同步影视观影状态 ...",
                            userid=event.event_data.get("user"),
                        )
                        self.sync_douban_status()
                        if event:
                            self.post_message(
                                channel=event.event_data.get("channel"),
                                title="同步飞牛影视观影状态完成！",
                                userid=event.event_data.get("user"),
                            )
                    elif event_data.get("action") == "sync_douban_to_trimmedia":
                        logger.info("收到命令，同步豆瓣已看到飞牛影视 ...")
                        self.post_message(
                            channel=event.event_data.get("channel"),
                            title="开始同步豆瓣已看 ...",
                            userid=event.event_data.get("user"),
                        )
                        self.reverse_sync_douban_status()
                        if event:
                            self.post_message(
                                channel=event.event_data.get("channel"),
                                title="同步豆瓣已看到飞牛影视完成！",
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
                    "id": "TrimMediaHelper",
                    "name": "飞牛影视豆瓣同步",
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


    def set_douban_done(self):
        logger.info("⏳ 开始同步已看状态...")
        watching_douban_id = []
        
        # 获取用户名，如果没有配置则使用默认值或返回错误
        username = self._trimmedia_user
        if not username:
            logger.error("飞牛影视用户名未配置")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title=f"【飞牛影视豆瓣同步】",
                    text=f"飞牛影视用户名未配置，请检查设置",
                )
            return
        
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            
            # 使用新的 SQL 查询语句
            sql = """
            SELECT 
                i2.imdb_id, i2.title
            FROM 
                item i2
            WHERE 
                i2.guid IN (
                    SELECT DISTINCT iup.item_guid
                    FROM item_user_play iup
                    INNER JOIN user u ON iup.user_guid = u.guid
                    WHERE iup.watched = 1
                      AND u.username = ?
                )
                AND i2.type IN ('Movie', 'TV', 'Season')
                AND i2.guid IN (
                    SELECT MIN(guid)
                    FROM item
                    WHERE type IN ('Movie', 'TV', 'Season')
                      AND imdb_id IS NOT NULL
                      AND imdb_id != ''
                    GROUP BY imdb_id
                    HAVING COUNT(*) >= 1
                )
            """
            
            cursor.execute(sql, (username,))
            results = cursor.fetchall()
            
            logger.info(f"查询到 {len(results)} 个已观看项目")
            
            # 进度跟踪变量
            total_items = len(results)
            processed_items = 0
            current_progress = 0
            
            for row in results:
                if self._should_stop:
                    logger.info("检测到中断请求，停止同步已看状态...")
                    break
                    
                imdb_id = row[0]
                title = row[1]
                
                # 更新进度
                processed_items += 1
                new_progress = int((processed_items / total_items) * 100)
                if new_progress > current_progress:
                    current_progress = new_progress
                    logger.info(f"处理进度: {current_progress}% ({processed_items}/{total_items})")

                # 先用 imdb_id 判断有没有处理过
                # 注意：这里需要确保缓存键是 imdb_id
                if self._cached_data.get(imdb_id) == DoubanStatus.DONE.value:
                    logger.info(f"ℹ️ 已处理过: {title} (IMDB: {imdb_id})，跳过...")
                    continue
                
                if not imdb_id or imdb_id == "":  # IMDb ID 为空直接跳过
                    logger.info(f"ℹ️ IMDb ID 为空: {title}，跳过...")
                    continue
                
                # 4. 调用 get_douban_id 函数将 imdb_id 转换为豆瓣ID
                douban_id = self._douban_helper.get_douban_id(imdb_id)
                                
                if not douban_id:  # 豆瓣ID 为 None、空字符串或 "0"
                    logger.info(f"ℹ️ 未找到豆瓣ID: {title} (IMDB: {imdb_id})，尝试通过标题搜索...")
                 
                if douban_id == "0":  # 豆瓣ID为0的直接跳过
                    logger.info(f"ℹ️ 豆瓣ID为0: {title} (IMDB: {imdb_id})，跳过...")
                    continue
                                
                if douban_id is not None:
                    watching_douban_id.append((imdb_id, douban_id, title))
                    logger.info(f"✅ 找到豆瓣ID: {title} -> 豆瓣ID: {douban_id} (IMDB: {imdb_id})")
                else:
                    logger.error(f"未找到豆瓣ID: {title} (IMDB: {imdb_id})")

        except sqlite3.Error as e:
            logger.error(f"数据库查询错误: {e}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title=f"【飞牛影视豆瓣同步】",
                    text=f"数据库查询错误: {e}",
                )
            return

        finally:
            # 确保游标和连接在使用完后关闭
            if cursor:
                cursor.close()
            if conn:
                conn.close()

        # 标记豆瓣已看状态
        message = ""
        total_to_process = len(watching_douban_id)
        processed_count = 0
        current_progress = 0
        
        for imdb_id, douban_id, title in watching_douban_id:
            if self._should_stop:
                logger.info("检测到中断请求，停止处理...")
                break
                
            processed_count += 1
            new_progress = int((processed_count / total_to_process) * 100)
            if new_progress > current_progress:
                current_progress = new_progress
                logger.info(f"标记进度: {current_progress}% ({processed_count}/{total_to_process})")
            
            status = DoubanStatus.DONE.value
            ret = self._douban_helper.set_watching_status(
                subject_id=douban_id, status=status, private=self._private
            )
            if ret:
                # 使用 imdb_id 作为缓存键
                self._cached_data[imdb_id] = status
                logger.info(f"✅ title: {title}, douban_id: {douban_id}, IMDb: {imdb_id}，已标记为已看")
                message += f"{title}，已标记为已看\n"
            else:
                logger.error(f"⚠️ title: {title}, douban_id: {douban_id}, IMDb: {imdb_id}，标记已看失败")
                message += f"{title}，***标记已看失败***\n"
            
            # 添加延迟避免请求过快
            time.sleep(1)
        
        if self._notify:
            if len(message) > 0:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【飞牛影视豆瓣同步】",
                    text=message,
                )
            # 发送完成通知
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【飞牛影视豆瓣同步】",
                text=f"已看状态同步完成！共处理 {processed_count}/{total_to_process} 个项目",
            )
            
        # 保存缓存数据
        self.save_data("trimmediahelper", self._cached_data)

        
    def reverse_sync_douban_status(self):
        logger.info(f"⏳ 开始同步豆瓣已看数据到飞牛影视...")
        
        # 检查必要的配置
        if not self._trimmedia_user:
            logger.error("飞牛影视用户名未配置")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【飞牛影视豆瓣同步】",
                    text="飞牛影视用户名未配置，请检查设置",
                )
            return
        
        if not self._douban_user:
            logger.error("豆瓣用户ID未配置")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【飞牛影视豆瓣同步】",
                    text="豆瓣用户ID未配置，请检查设置",
                )
            return
        
        # 连接到数据库
        conn = sqlite3.connect(self._db_path)
        conn.text_factory = str
        cursor = conn.cursor()
        
        processed_count = 0
        skipped_count = 0
        error_count = 0
        
        try:
            # 先获取一次 user_guid（只执行一次）
            cursor.execute("""
                SELECT guid as user_guid
                FROM user
                WHERE username = ?
            """, (self._trimmedia_user,))
            
            user_result = cursor.fetchone()
            if not user_result:
                logger.error(f"未找到用户 {self._trimmedia_user}")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【飞牛影视豆瓣同步】",
                        text=f"未找到飞牛影视用户 {self._trimmedia_user}，请检查用户名",
                    )
                return
            
            user_guid = user_result[0]
            logger.info(f"获取到用户 {self._trimmedia_user} 的 GUID: {user_guid}")
            
            # 1. 通过get_user_movies获得用户在豆瓣上全部的已看数据
            logger.info(f"正在获取豆瓣用户 {self._douban_user} 的已看电影数据...")
            douban_movies = self._douban_helper.get_user_movies(
                douban_user=self._douban_user, 
                status='collect'
            )
            
            if not douban_movies:
                logger.warning("未获取到豆瓣已看电影数据")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【飞牛影视豆瓣同步】",
                        text="未获取到豆瓣已看电影数据，请检查豆瓣用户ID和cookie配置",
                    )
                return
            
            logger.info(f"获取到 {len(douban_movies)} 部豆瓣已看电影")
            
            # 2. 遍历get_user_movies返回的所有电影数据，逐条处理
            for movie in douban_movies:
                if self._should_stop:
                    logger.info("检测到中断请求，停止同步已看状态...")
                    break
                
                douban_id = movie.get('douban_id')
                imdb_id = movie.get('imdb_id')
                title = movie.get('title')
                
                if not imdb_id or imdb_id == "":
                    logger.info(f"ℹ️ 跳过 {title} (豆瓣ID: {douban_id}) - 无IMDb ID")
                    skipped_count += 1
                    continue
                
                logger.info(f"正在处理: {title} (豆瓣ID: {douban_id}, IMDb: {imdb_id})")
                
                # 检查缓存：如果已经处理过这个IMDb ID，则跳过
                # 使用相同的缓存键和判断逻辑，与set_douban_done保持一致
                if self._cached_data.get(imdb_id) == DoubanStatus.DONE.value:
                    logger.info(f"ℹ️ 已处理过: {title} (IMDB: {imdb_id})，跳过...")
                    skipped_count += 1
                    continue
                
                try:
                    # 第一步：查询获取guid和type
                    cursor.execute("""
                        SELECT guid, type
                        FROM item
                        WHERE imdb_id = ?
                          AND type IN ('Movie', 'Episode', 'Season')
                    """, (imdb_id,))
                    
                    items = cursor.fetchall()
                    
                    if not items:
                        logger.info(f"ℹ️ 未找到IMDb ID为 {imdb_id} 的项目: {title}")
                        skipped_count += 1
                        continue
                    
                    logger.info(f"找到 {len(items)} 个匹配项目")
                    
                    has_processed = False
                    
                    # 第三步：对于每个查询到的(guid, type)组合，执行处理逻辑
                    for item_guid, item_type in items:
                        # 先查询是否已存在
                        cursor.execute("""
                            SELECT item_guid, watched 
                            FROM item_user_play 
                            WHERE item_guid = ?
                              AND user_guid = ?
                        """, (item_guid, user_guid))
                        
                        existing_record = cursor.fetchone()
                        
                        if existing_record:
                            item_guid_found, watched = existing_record
                            if watched == 0:
                                # 如果存在且watched=0，执行更新
                                cursor.execute("""
                                    UPDATE item_user_play 
                                    SET watched = 1, 
                                        update_time = CAST(strftime('%s', 'now') AS INTEGER)
                                    WHERE item_guid = ? 
                                      AND user_guid = ?
                                """, (item_guid, user_guid))
                                logger.info(f"✅ 更新观看状态: {title} (guid: {item_guid})")
                                has_processed = True
                            else:
                                logger.info(f"ℹ️ 已标记为已观看: {title} (guid: {item_guid})")
                        else:
                            # 如果不存在，执行插入
                            cursor.execute("""
                                INSERT INTO item_user_play (
                                    item_guid, user_guid, ts, watched, 
                                    media_guid, video_guid, audio_guid, subtitle_guid,
                                    direct_link_audio_index, resolution, bitrate, type,
                                    visible, create_time, update_time
                                ) VALUES (
                                    ?, ?, 0, 1,
                                    NULL, NULL, NULL, NULL,
                                    -1, NULL, 0, ?,
                                    1, CAST(strftime('%s', 'now') AS INTEGER), CAST(strftime('%s', 'now') AS INTEGER)
                                )
                            """, (item_guid, user_guid, item_type))
                            logger.info(f"✅ 插入观看记录: {title} (guid: {item_guid}, type: {item_type})")
                            has_processed = True
                    
                    # 如果至少处理了一个项目，则更新计数和缓存
                    if has_processed:
                        processed_count += 1
                        # 添加到缓存，使用相同的键和值
                        self._cached_data[imdb_id] = DoubanStatus.DONE.value
                    else:
                        skipped_count += 1
                    
                    # 提交当前处理的事务
                    conn.commit()
                    
                except sqlite3.Error as e:
                    logger.error(f"❌ 处理 {title} 时数据库错误: {e}")
                    conn.rollback()
                    error_count += 1
                except Exception as e:
                    logger.error(f"❌ 处理 {title} 时发生错误: {e}")
                    conn.rollback()
                    error_count += 1
                
                # 添加延迟，避免请求过快
                time.sleep(0.5)
            
            # 输出统计信息
            logger.info(f"同步完成统计:")
            logger.info(f"  成功处理: {processed_count} 条")
            logger.info(f"  跳过处理: {skipped_count} 条")
            logger.info(f"  处理失败: {error_count} 条")
            
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【飞牛影视豆瓣同步】",
                    text=f"豆瓣已看数据同步完成！\n成功处理: {processed_count} 条\n跳过: {skipped_count} 条\n失败: {error_count} 条",
                )
                
            # 保存缓存数据
            self.save_data("trimmediahelper", self._cached_data)
                
        except Exception as e:
            logger.error(f"❌ 同步过程中发生严重错误: {e}")
            conn.rollback()
            
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【飞牛影视豆瓣同步】",
                    text=f"同步过程中发生错误: {str(e)[:100]}...",
                )
            raise
            
        finally:
            # 关闭数据库连接
            conn.close()
        
        # 同步豆瓣到飞牛影视为一次性任务，完成后关闭选项
        self._reverse_sync_douban_status = False
        self._update_config()
        
        logger.info("豆瓣已看数据同步到飞牛影视完成")

    def sync_douban_status(self):
        self.set_douban_watching()
        self.set_douban_done()

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
                                            "label": "单向同步（c影视->豆瓣）",
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
                                            "label": "双向同步（豆瓣-飞牛影视）",
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
                                            "model": "trimmedia_user",
                                            "label": "飞牛影视用户名",
                                            "placeholder": "填入飞牛影视用户名。",
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
                                            "label": "飞牛影视数据库路径",
                                            "rows": 1,
                                            "placeholder": "飞牛影视数据库路径为/usr/local/apps/@appdata/trim.media/database/trimmedia.db，需先映射路径",
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
                                            "text": "本插件基于飞牛影视数据库扩展功能，需开启SSH后通过Portainer等工具映射飞牛影视数据库路径",
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
                                            "text": "双向同步仅执行一次，执行后会自动关闭。该选项会先将豆瓣已看数据同步到飞牛影视中。",
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
                                            "text": "飞牛影视用户名，用于同步豆瓣已看至飞牛影视。飞牛影视数据库的观看状态需要对应用户名。",
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
