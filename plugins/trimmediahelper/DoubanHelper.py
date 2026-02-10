import re
from typing import Tuple
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup
from http.cookies import SimpleCookie
from app.helper.cookiecloud import CookieCloudHelper
from app.log import logger

import time
import json
import random

class DoubanHelper:

    def __init__(self, user_cookie: str = None):
        if not user_cookie:
            self.cookiecloud = CookieCloudHelper()
            cookie_dict, msg = self.cookiecloud.download()
            if cookie_dict is None:
                logger.error(f"获取cookiecloud数据错误 {msg}")
            self.cookies = cookie_dict.get("douban.com")
        else:
            self.cookies = user_cookie
        self.cookies = {k: v.value for k, v in SimpleCookie(self.cookies).items()}
        user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36 Edg/113.0.1774.57'
        self.headers = {
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate, sdch',
            'Accept-Language': 'zh-CN,zh;q=0.8,en-US;q=0.6,en;q=0.4,en-GB;q=0.2,zh-TW;q=0.2',
            'Connection': 'keep-alive',
            'DNT': '1',
        }

        if self.cookies.get('__utmz'):
            self.cookies.pop("__utmz")

        # 移除用户传进来的comment-key
        if self.cookies.get('ck'):
            self.cookies.pop("ck")

        # 获取最新的ck
        self.set_ck()

        self.ck = self.cookies.get('ck')
        logger.debug(f"ck:{self.ck} cookie:{self.cookies}")

        if not self.cookies:
            logger.error(f"cookie获取为空，请检查插件配置或cookie cloud")
        if not self.ck:
            logger.error(f"请求ck失败，请检查传入的cookie登录状态")

    def set_ck(self):
        self.headers["Cookie"] = ";".join([f"{key}={value}" for key, value in self.cookies.items()])
        response = requests.get("https://www.douban.com/", headers=self.headers)
        ck_str = response.headers.get('Set-Cookie', '')
        logger.debug(ck_str)
        if not ck_str:
            logger.error('获取ck失败，检查豆瓣登录状态')
            self.cookies['ck'] = ''
            return
        cookie_parts = ck_str.split(";")
        ck = cookie_parts[0].split("=")[1].strip()
        logger.debug(ck)
        self.cookies['ck'] = ck

    def get_douban_id(self, imdb_id: str) -> str:
        # 基础随机延迟 7-11 秒,避免被豆瓣反爬
        time.sleep(random.uniform(9, 13))
        url = f"https://www.douban.com/search?cat=1002&q={imdb_id}"
        print(f"请求URL: {url}")
        response = requests.get(url, headers=self.headers, cookies=self.cookies)
        try:
            response = requests.get(url, headers=self.headers, cookies=self.cookies, timeout=10)
            if response.status_code != 200:
                print(f"搜索 IMDb ID {imdb_id} 失败，状态码：{response.status_code}")
                return None
                
            soup = BeautifulSoup(response.text, 'html.parser')
            title_divs = soup.find_all("div", class_="title")
            
            if not title_divs:
                print(f"找不到 IMDb ID {imdb_id} 相关条目")
                return None
            
            # 查找第一个有效的结果
            for div in title_divs:
                a_tag = div.find_all("a")
                if not a_tag:
                    continue
                    
                link = unquote(a_tag[0]["href"])
                if "subject/" in link:
                    pattern = r"subject/(\d+)/"
                    match = re.search(pattern, link)
                    if match:
                        douban_id = match.group(1)
                        # 验证是否是我们要找的内容
                        title = a_tag[0].string.strip() if a_tag[0].string else ""
                        print(f"找到匹配: {title} -> 豆瓣ID: {douban_id} (IMDb: {imdb_id})")
                        return douban_id
            
            print(f"IMDb ID {imdb_id} 的搜索结果中没有找到豆瓣链接")
            return None
            
        except requests.RequestException as e:
            print(f"请求豆瓣搜索失败: {e}")
            return None
        except Exception as e:
            print(f"解析搜索结果失败: {e}")
            return None

    def set_watching_status(self, subject_id: str, status: str = "do", private: bool = True) -> bool:
        self.headers["Referer"] = f"https://movie.douban.com/subject/{subject_id}/"
        self.headers["Origin"] = "https://movie.douban.com"
        self.headers["Host"] = "movie.douban.com"
        self.headers["Cookie"] = ";".join([f"{key}={value}" for key, value in self.cookies.items()])
        data_json = {
            "ck": self.ck,
            "interest": "do",
            "rating": "",
            "foldcollect": "U",
            "tags": "",
            "comment": ""
        }
        if private:
            data_json["private"] = "on"
        data_json["interest"] = status
        response = requests.post(
            url=f"https://movie.douban.com/j/subject/{subject_id}/interest",
            headers=self.headers,
            data=data_json)
        if not response:
            return False
        if response.status_code == 200:
            # 正常情况 {"r":0}
            ret = response.json().get("r")
            r = False if (isinstance(ret, bool) and ret is False) else True
            if r:
                return True
            # 未开播 {"r": false}
            else:
                logger.error(f"douban_id: {subject_id} 未开播")
                return False
        logger.error(response.text)
        return False

    # ---------------------------
    # 通用工具
    # ---------------------------
    
    def clean_title(self, text: str) -> str:
        text = text.strip()
        text = text.split("\n")[0]
        text = text.split("/")[0]
        text = text.replace("[可播放]", "")
        return text.strip()
    
    
    def extract_douban_id(self, url: str) -> str | None:
        m = re.search(r"/subject/(\d+)/", url)
        return m.group(1) if m else None
    
    
    def extract_chinese_name(self, name: str) -> str | None:
        parts = re.findall(r"[\u4e00-\u9fff]+", name)
        if not parts:
            return None
        return "".join(parts)
    

     HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
       'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Referer': 'https://movie.douban.com/',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }

    def get_user_movies(self, username: str, status: str = "collect"):
        movies = []
        start = 0
        
        # 状态映射
        status_map = {
            "collect": "已看",
            "do": "在看",
            "wish": "想看"
        }
        logger.info(f"开始获取用户 {username} 的 {status_map.get(status, status)} 列表...")

        while True:
            url = f"https://movie.douban.com/people/{username}/{status}"
            params = {
                "start": start,
                "sort": "time",
                "rating": "all",
                "filter": "all",
                "mode": "grid"
            }               
            resp = requests.get(url, headers=self.headers, cookies=self.cookies, params=params, timeout=10)
            resp.raise_for_status()

            try:
                resp = requests.get(url, headers=self.headers, cookies=self.cookies, params=params, timeout=10)
                resp.raise_for_status()
            except Exception as e:
                print(f"请求列表失败 {url}: {e}")
                break

            soup = BeautifulSoup(resp.text, 'html.parser')
            items = soup.find_all('div', class_='item')
            if not items:
                # 额外检查是否被反爬（页面有“验证”或登录提示）
                if "请验证" in resp.text or "登录" in resp.text:
                    print("豆瓣检测到爬虫或需要登录，请检查您的收藏是否公开！")
                logger.info(f"{status_map.get(status, status)} 列表获取完毕（本页无数据）")
                break

            print(f"第 {start // 15 + 1} 页，获取到 {len(items)} 条")
            for item in items:
                link_elem = item.find('a', class_='nbg')
                if not link_elem:
                    continue
                link = link_elem['href']
                title_full = item.find('em').text.strip()
                # 优先提取中文
                simplified_title = ' '.join(re.findall(r'[\u4e00-\u9fa5]+', title_full))
                if not simplified_title:
                    simplified_title = title_full.split(' / ')[0].strip()
                douban_id = link.strip('/').split('/')[-1]

                imdb_id = get_imdb_id(link)
                movies.append({
                    'douban_id': douban_id,
                    'title': simplified_title,
                    'imdb_id': imdb_id, 
                    'status': status_map.get(status, status)
                })

            start += 15
            time.sleep(random.uniform(5, 10))

        logger.info(f"{status_map.get(status, status)} 共获取 {len(movies)} 条")
        return movies

    def get_imdb_id(self, url: str) -> str:
        """
        从豆瓣电影页面获取 IMDb ID，包含基础防反爬延迟
        返回完整的 IMDb ID（包含 'tt' 前缀）
        """
        try:
            # 基础随机延迟 1-3 秒
            time.sleep(random.uniform(1, 2))
            
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            info = soup.find('div', id='info')
            
            if info:
                for span in info.find_all('span', class_='pl'):
                    # 更宽松的匹配，豆瓣可能有中文冒号或空格
                    span_text = span.get_text(strip=True)
                    if 'IMDb:' in span_text or 'IMDb：' in span_text:
                        # 查找 IMDb ID
                        next_sibling = span.next_sibling
                        # 可能需要跳过空白节点
                        while next_sibling and (not next_sibling.string or next_sibling.string.strip() == ''):
                            next_sibling = next_sibling.next_sibling
                        
                        if next_sibling and next_sibling.string:
                            imdb_id = next_sibling.string.strip()
                            logger.debug(f"从 {url} 获取到 IMDb ID: {imdb_id}")
                            return imdb_id
            
            logger.debug(f"未在 {url} 中找到 IMDb ID")
            return None
            
        except requests.RequestException as e:
            logger.warning(f"请求豆瓣页面失败 {url}: {e}")
            return None
        except Exception as e:
            logger.warning(f"解析豆瓣页面失败 {url}: {e}")
            return None
        
if __name__ == "__main__":
    doubanHelper = DoubanHelper()
    subject_title, subject_id, score = doubanHelper.get_subject_id("火线 第 3 季")
    logger.info(f"subject_title: {subject_title}, subject_id: {subject_id}, score: {score}")
    doubanHelper.set_watching_status(subject_id=subject_id, status="do", private=True)
