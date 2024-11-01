import requests
import json
import hashlib
import time
import urllib.parse
from bs4 import BeautifulSoup

def get_forum_fid(title):
    """
    获取指定贴吧的 fid
    """
    url = f'http://tieba.baidu.com/f/commit/share/fnameShareApi?ie=utf-8&fname={urllib.parse.quote(title)}'
    response = requests.get(url)
    if response.status_code == 200:
        try:
            data = response.json()
            if data.get("no") == 0:
                return data["data"]["fid"]
            else:
                print(f"获取 {title} 的 fid 失败，错误信息：{data.get('error')}")
        except json.JSONDecodeError:
            print(f"解析 {title} 的响应 JSON 失败")
    else:
        print(f"请求 {title} 失败，状态码：{response.status_code}")
    return None

def get_all_forums_fid(bduss, stoken):
    print("正在获取所有关注的贴吧...")
    """
    循环获取用户所有关注贴吧的 fid 并保存到 tieba.json
    """
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Cookie': f'BDUSS={bduss}; STOKEN={stoken};'
    }
    url = 'https://tieba.baidu.com/f/like/mylike?&pn=1'
    response = requests.get(url, headers=headers)

    all_titles = []
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        # last_page_link = soup.find('a', text="尾页")
        last_page_link = soup.find('a', string="尾页")  # 修改这里
        last_page_num = int(last_page_link['href'].split('pn=')[-1]) if last_page_link else 1

        for pn in range(1, last_page_num + 1):
            headers['Referer'] = f'https://tieba.baidu.com/f/like/mylike?&pn={pn}'
            page_url = f'https://tieba.baidu.com/f/like/mylike?&pn={pn}'
            response = requests.get(page_url, headers=headers)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                table_rows = soup.select('.forum_table table tr')[1:]
                for row in table_rows:
                    title_tag = row.find('a', title=True)
                    if title_tag:
                        all_titles.append(title_tag['title'])

    results = [{"title": title, "fid": get_forum_fid(title)} for title in all_titles if get_forum_fid(title)]
    with open('tieba.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"结果已保存到 tieba.json 文件，包含 {len(results)} 个贴吧")

def sign_forum(BDUSS, tiebainfo, tbs):
    url = "http://c.tieba.baidu.com/c/c/forum/sign"
    kw = tiebainfo.get("title")
    data = {
        "BDUSS": BDUSS,
        "fid": tiebainfo.get("fid"),
        "kw": kw,
        "tbs": tbs,
    }
    SIGN_KEY = "tiebaclient!!!"
    sign_str = "".join([f"{k}={data[k]}" for k in sorted(data.keys())]) + SIGN_KEY
    sign = hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()
    data.update({"sign": sign})

    headers = {"Cookie": "BDUSS=" + BDUSS}

    try:
        r = requests.post(url, headers=headers, data=data, timeout=8)
        res = r.json()
    except (requests.ConnectionError, requests.ReadTimeout, requests.ConnectTimeout) as e:
        res = {"error_msg": f"{e}"}

    if res.get("error_code") == "0":
        msg = "√"
    elif res.get("error_code") == "160002":
        msg = "已经签到过了"
    else:
        msg = f"× [error_msg]: {res.get('error_msg', None)}"
    print(f"签到 {kw:<14}吧   {msg}")

    return res

def sign_all_forums(BDUSS):
    try:
        with open("tieba.json", "r", encoding="utf-8") as file:
            tieba_list = json.load(file)
    except FileNotFoundError:
        print("未找到 tieba.json 文件。请确保文件存在。")
        return

    tbs_url = "http://tieba.baidu.com/dc/common/tbs"
    headers = {"Cookie": "BDUSS=" + BDUSS}
    tbs_response = requests.get(tbs_url, headers=headers)
    tbs = tbs_response.json().get("tbs")

    if not tbs:
        print("获取 tbs 失败，请检查 BDUSS 是否有效。")
        return

    start_time = time.time()
    print(f"共需签到 {len(tieba_list)} 个贴吧")
    for tiebainfo in tieba_list:
        sign_forum(BDUSS, tiebainfo, tbs)
    print(f"签到完成，用时 {time.time() - start_time:.2f} 秒")






# 示例调用
bduss = "你的BDUSS值"
stoken = "你的STOKEN值"
get_all_forums_fid(bduss, stoken)#用过一次后如果不想要了可以注释掉
sign_all_forums(bduss)
