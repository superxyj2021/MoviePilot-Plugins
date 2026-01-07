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

    def get_subject_id(self, title: str) -> Tuple[str, str, str]:
        url = f"https://www.douban.com/search?cat=1002&q={title}"
        print(f"请求URL: {url}")
        response = requests.get(url, headers=self.headers, cookies=self.cookies)
        if response.status_code != 200:
            print(f"搜索 {title} 失败 状态码：{response.status_code}")
            return None, None, None
        soup = BeautifulSoup(response.text, 'html.parser')
        title_divs = soup.find_all("div", class_="title")
        subject_items = []
        for div in title_divs:
            item = {}
            a_tag = div.find_all("a")[0]
            item["title"] = a_tag.string.strip()
            if len(div.find_all(class_="subject-cast")) == 0:
                continue
            span_tag = div.find_all(class_="subject-cast")[0]
            year = span_tag.string[-4:]
            if year.isdigit():
                item["year"] = year
            rating_nums = div.find_all(class_="rating_nums")
            if rating_nums:
                item["rating_nums"] = rating_nums[0].string
            else:
                item["rating_nums"] = "0"
            link = unquote(a_tag["href"])
            if "subject/" in link:
                pattern = r"subject/(\d+)/"
                match = re.search(pattern, link)
                if match:
                    item["subject_id"] = match.group(1)
            subject_items.append(item)
        if not subject_items:
            print(f"找不到 {title} 相关条目")
            return None, None, None
        for subject_item in subject_items:
            print(f"找到: {subject_item['title']} {subject_item['subject_id']} 评分: {subject_item['rating_nums']}")
            return subject_item["title"], subject_item["subject_id"], subject_item["rating_nums"]
        return None, None, None

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
    
    
    # ---------------------------
    # 用户全部影视（含评分日期）
    # ---------------------------
    
    def fetch_all_movies(self, douban_user: str = None):
        #statuses = ["collect", "wish"]
        statuses = ["collect"]
        for status in statuses:
            start = 0
            empty_pages = 0
    
            while True:
                logger.info(f"⏳ 抓取豆瓣影音记录，start={start}")

                url = f"https://movie.douban.com/people/{douban_user}/{status}"
                params = {
                    "start": start,
                    "sort": "time",
                    "rating": "all",
                    "filter": "all",
                    "mode": "grid"
                }               
                resp = requests.get(url, headers=self.headers, cookies=self.cookies, params=params, timeout=10)
                resp.raise_for_status()
    
                soup = BeautifulSoup(resp.text, "html.parser")
                items = soup.select(".item")
    
                if not items:
                    empty_pages += 1
                    if empty_pages >= 2:
                        break
                else:
                    empty_pages = 0
    
                for item in items:
                    link_el = item.select_one(".info a")
                    if not link_el:
                        continue
    
                    title = self.clean_title(link_el.text)
                    detail_url = link_el["href"]
                    douban_id = self.extract_douban_id(detail_url)
    
                    # ⭐ 评分日期（只在“看过”里有）
                    rating_date = None
                    if status == "collect":
                        date_el = item.select_one(".date")
                        if date_el:
                            rating_date = date_el.text.strip()
                            
                    yield {
                        "douban_id": douban_id,
                        "title": title,
                        "status": "看过" if status == "collect" else "想看",
                        "rating_date": rating_date,
                    }
    
                    time.sleep(1)
    
                start += 15
                time.sleep(2)
            


if __name__ == "__main__":
    doubanHelper = DoubanHelper()
    subject_title, subject_id, score = doubanHelper.get_subject_id("火线 第 3 季")
    logger.info(f"subject_title: {subject_title}, subject_id: {subject_id}, score: {score}")
    doubanHelper.set_watching_status(subject_id=subject_id, status="do", private=True)
