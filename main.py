"""贴吧全量关注列表获取 + 手机端签到。

设计要点
--------
* 凭据来自环境变量 / .env 文件 / 交互式输入，不写死在源码里；
* TiebaClient 封装所有网络行为：统一超时、指数退避重试、请求限速；
* tieba.json 是增量合并的缓存，抓取不完整时只增不删（详见 sync_forums）；
* 写入是原子的（.tmp + os.replace），覆盖前自动备份 .bak；
* 日志走 logging，-v 看细节，--quiet 只留错误。

用法
----
    python3 main.py                # 更新关注列表 + 签到
    python3 main.py refresh        # 只更新 tieba.json
    python3 main.py sign           # 只签到
    python3 main.py refresh --no-prune --report run.json
"""

import argparse
import getpass
import hashlib
import json
import logging
import os
import random
import shutil
import sys
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger("tieba")

# ------------------------------------------------------------------ 配置常量

DATA_FILE = "tieba.json"
BACKUP_FILE = "tieba.json.bak"
ENV_FILE = ".env"

DEFAULT_TIMEOUT = 8.0        # 单次请求超时（秒）
DEFAULT_INTERVAL = 0.4       # 相邻请求的最小间隔（秒），降低被风控的概率
DEFAULT_RETRIES = 2          # 失败重试次数（不含首次）
SHRINK_GUARD = 0.5           # 抓到的数量少于缓存的这个比例时，视为抓取异常
MAX_PAGES = 1000             # 页码上限，防止"尾页"解析异常时死循环
BACKOFF_BASE = 0.8           # 指数退避基数
BACKOFF_CAP = 8.0

SIGN_KEY = "tiebaclient!!!"
SIGN_URL = "http://c.tieba.baidu.com/c/c/forum/sign"
TBS_URL = "https://tieba.baidu.com/dc/common/tbs"
MYLIKE_URL = "https://tieba.baidu.com/f/like/mylike?pn={}"
FNAME_API = "https://tieba.baidu.com/f/commit/share/fnameShareApi"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36"
)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

ENV_BDUSS = "TIEBA_BDUSS"
ENV_STOKEN = "TIEBA_STOKEN"


# ------------------------------------------------------------------ 凭据

def load_env_file(path=ENV_FILE):
    """把 .env 里的 KEY=VALUE 读进环境变量（已存在的变量不覆盖）。

    返回实际导入的条数。
    """
    if not os.path.isfile(path):
        return 0

    imported = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        LOG.warning("读取 %s 失败：%s", path, e)
        return 0

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            imported += 1
    return imported


def resolve_credential(env_name, prompt, interactive):
    """环境变量优先；缺失且可交互时用 getpass 现问。"""
    value = os.environ.get(env_name, "").strip()
    if value or not interactive:
        return value
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return ""


# ------------------------------------------------------------------ 网络客户端

class TiebaClient:
    """把贴吧的所有 HTTP 细节收在一个地方。

    参数
    ----
    bduss / stoken : 凭据，留空则不设置对应 Cookie
    timeout        : 单次请求超时
    interval       : 相邻请求最小间隔（限速）
    retries        : 失败重试次数
    session        : 可注入的 session（测试用）
    """

    def __init__(self, bduss="", stoken="", timeout=DEFAULT_TIMEOUT,
                 interval=DEFAULT_INTERVAL, retries=DEFAULT_RETRIES, session=None):
        self.bduss = bduss or ""
        self.stoken = stoken or ""
        self.timeout = timeout
        self.interval = interval
        self.retries = max(0, int(retries))
        self.session = session if session is not None else self._build_session()
        self._last_request = 0.0

        if self.bduss:
            self.session.cookies.set("BDUSS", self.bduss, domain=".tieba.baidu.com")
        if self.stoken:
            self.session.cookies.set("STOKEN", self.stoken, domain=".tieba.baidu.com")

    # -- 内部工具 ---------------------------------------------------

    @staticmethod
    def _build_session():
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Referer": "https://tieba.baidu.com/",
        })
        return session

    def _throttle(self):
        if self.interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_request = time.monotonic()

    @staticmethod
    def _backoff(attempt):
        delay = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** (attempt - 1)))
        time.sleep(delay + random.uniform(0, 0.3))

    def _request(self, method, url, **kwargs):
        """带限速与退避重试的请求；失败返回 None，绝不抛出网络异常。"""
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as e:
                LOG.warning("%s %s 失败（%s），第 %d/%d 次",
                            method, url, e, attempt, attempts)
            else:
                if response.status_code in RETRY_STATUS and attempt < attempts:
                    LOG.warning("%s %s 返回 %d，准备重试",
                                method, url, response.status_code)
                elif response.status_code >= 400:
                    LOG.error("%s %s 返回 %d", method, url, response.status_code)
                    return None
                else:
                    return response
            if attempt < attempts:
                self._backoff(attempt)
        return None

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError:
            LOG.warning("返回内容不是 JSON（可能被拦截或重定向）")
            return {}

    # -- 业务接口 ---------------------------------------------------

    def followed_titles(self):
        """抓取全部关注的吧名。

        返回 (titles, complete)。complete=False 表示有页面请求/解析异常，
        调用方应当只做合并、不要删除任何旧条目。
        """
        first = self._request("GET", MYLIKE_URL.format(1))
        if first is None:
            return [], False

        soup = BeautifulSoup(first.text, "html.parser")
        if soup.select_one(".forum_table") is None:
            LOG.error("关注列表页面结构异常（可能登录已失效或被跳到登录页），"
                      "本次不修改 %s", DATA_FILE)
            return [], False

        last_page = _parse_last_page(soup)
        LOG.info("共 %d 页关注列表", last_page)

        titles = []
        complete = True
        for pn in range(1, last_page + 1):
            if pn == 1:
                response = first
            else:
                response = self._request("GET", MYLIKE_URL.format(pn))
                if response is None:
                    LOG.warning("第 %d 页请求失败", pn)
                    complete = False
                    continue

            page_titles = _parse_titles(response.text)
            if not page_titles and last_page > 1:
                LOG.warning("第 %d 页没有解析到任何贴吧，判定本次抓取不完整", pn)
                complete = False
            titles.extend(page_titles)

        return list(dict.fromkeys(titles)), complete

    def forum_fid(self, title):
        """通过 fnameShareApi 把吧名换成 fid，失败返回 None。"""
        response = self._request("GET", FNAME_API, params={"ie": "utf-8", "fname": title})
        if response is None:
            LOG.warning("  %s: 获取 fid 失败（网络）", title)
            return None

        data = self._json(response)
        if data.get("no") == 0:
            return data["data"]["fid"]
        LOG.warning("  %s: 获取 fid 失败，%s", title, data.get("error"))
        return None

    def fetch_tbs(self):
        response = self._request("GET", TBS_URL)
        if response is None:
            return None
        tbs = self._json(response).get("tbs")
        if not tbs:
            LOG.error("获取 tbs 失败，%s 可能已失效", ENV_BDUSS)
            return None
        return tbs

    def sign(self, forum, tbs):
        """模拟手机端签到单个贴吧，返回接口原始响应字典。"""
        kw = forum.get("title") or ""
        data = {
            "BDUSS": self.bduss,
            "fid": forum.get("fid"),
            "kw": kw,
            "tbs": tbs,
        }
        sign_str = "".join(f"{k}={data[k]}" for k in sorted(data)) + SIGN_KEY
        data["sign"] = hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()

        response = self._request("POST", SIGN_URL, data=data)
        if response is None:
            LOG.info("签到 %-16s × 网络失败", _forum_label(kw))
            return {"error_msg": "network error"}

        res = self._json(response)
        code = str(res.get("error_code", ""))
        if code == "0":
            msg = "√"
        elif code == "160002":
            msg = "已经签到过了"
        else:
            msg = f"× [error_msg]: {res.get('error_msg')}"
        LOG.info("签到 %-16s %s", _forum_label(kw), msg)
        return res


def _forum_label(title):
    return title if title.endswith("吧") else f"{title}吧"


# ------------------------------------------------------------------ HTML 解析

def _parse_last_page(soup):
    """从"尾页"链接里解析总页数，解析失败按 1 处理。"""
    for a in soup.find_all("a"):
        if a.get_text(strip=True) == "尾页" and a.get("href"):
            query = urllib.parse.urlparse(a["href"]).query
            pn = urllib.parse.parse_qs(query).get("pn")
            if pn and pn[0].isdigit():
                return max(1, min(int(pn[0]), MAX_PAGES))
    return 1


def _parse_titles(html):
    """解析一页"我关注的吧"，返回吧名列表。"""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select(".forum_table table tr")[1:]

    titles = []
    if rows:
        for row in rows:
            tag = row.find("a", title=True)
            if tag:
                titles.append(tag["title"].strip())
    else:
        for tag in soup.select(".forum_table a[title]"):
            titles.append(tag["title"].strip())
    return titles


# ------------------------------------------------------------------ 缓存读写

def load_cache(path=DATA_FILE):
    """读取 tieba.json，返回 {吧名: fid}，保持文件里的原顺序。

    文件不存在返回空字典；文件损坏抛 ValueError（绝不静默覆盖旧数据）。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"{path} 读取/解析失败：{e}") from e

    if not isinstance(raw, list):
        raise ValueError(f"{path} 格式不对，期望是列表")

    cache = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        fid = item.get("fid")
        if title and fid:
            cache[title] = fid
    return cache


def save_cache(cache, path=DATA_FILE, backup_path=BACKUP_FILE):
    """原子写入：先写 .tmp 再 os.replace，并在覆盖前备份旧文件。"""
    payload = [{"title": t, "fid": f} for t, f in cache.items()]
    tmp_path = f"{path}.tmp"

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    if os.path.exists(path):
        shutil.copy2(path, backup_path)
    os.replace(tmp_path, path)
    return len(payload)


# ------------------------------------------------------------------ 合并策略

def sync_forums(cache, titles, complete, lookup, prune=True):
    """把本次抓到的吧名合并进缓存。

    cache    : {吧名: fid} 现有缓存
    titles   : 本次抓到的吧名列表（可能不完整）
    complete : 本次抓取是否完整
    lookup   : 吧名 -> fid 的查询函数，只在缓存里没有时调用
    prune    : 允许在抓取完整时删除已取关的吧

    返回 (new_cache, stats)。
    """
    fetched = list(dict.fromkeys(titles))
    fetched_set = set(fetched)
    stats = {"added": [], "removed": [], "failed": [], "kept": [], "reused": 0}

    shrinking = bool(cache) and len(fetched) < len(cache) * SHRINK_GUARD
    allow_prune = prune and complete and not shrinking
    if prune and not allow_prune:
        reason = ("抓取不完整" if not complete
                  else f"结果仅 {len(fetched)} 条，远少于缓存的 {len(cache)} 条")
        LOG.warning("⚠ %s，本次只新增/保留，不删除任何贴吧", reason)

    new_cache = {}

    # 1. 缓存里已有且本次还在列表里的，直接复用 fid（保持原顺序）
    for title, fid in cache.items():
        if title in fetched_set:
            new_cache[title] = fid
            stats["reused"] += 1

    # 2. 新出现的吧，才去查 fid
    for title in fetched:
        if title in new_cache:
            continue
        fid = lookup(title)
        if fid:
            new_cache[title] = fid
            stats["added"].append(title)
        else:
            stats["failed"].append(title)

    # 3. 本次没抓到的旧条目：允许删除就删，否则原样保留在末尾
    for title, fid in cache.items():
        if title in new_cache:
            continue
        if allow_prune:
            stats["removed"].append(title)
        else:
            new_cache[title] = fid
            stats["kept"].append(title)

    return new_cache, stats


def log_stats(old_count, new_cache, stats):
    LOG.info("tieba.json: %d → %d 个贴吧（复用 %d，新增 %d，移除 %d，"
             "fid 获取失败 %d，保留 %d）",
             old_count, len(new_cache), stats["reused"], len(stats["added"]),
             len(stats["removed"]), len(stats["failed"]), len(stats["kept"]))

    def _preview(items):
        text = ", ".join(items[:20])
        return text + (" …" if len(items) > 20 else "")

    if stats["added"]:
        LOG.info("  新增：%s", _preview(stats["added"]))
    if stats["removed"]:
        LOG.info("  移除：%s", _preview(stats["removed"]))
    if stats["failed"]:
        LOG.info("  fid 获取失败的（下次会重试）：%s", _preview(stats["failed"]))
    if stats["kept"]:
        LOG.info("  本次没抓到但已保留：%s", _preview(stats["kept"]))


# ------------------------------------------------------------------ 命令实现

def cmd_refresh(client, data=DATA_FILE, prune=True):
    """更新 tieba.json，返回 (cache, stats)。"""
    cache = load_cache(data)
    LOG.info("缓存中已有 %d 个贴吧", len(cache))

    titles, complete = client.followed_titles()
    if not titles:
        LOG.error("没有抓到任何贴吧，%s 保持不变", data)
        return cache, None

    LOG.info("抓到 %d 个关注的贴吧（%s）",
             len(titles), "完整" if complete else "不完整")
    new_cache, stats = sync_forums(cache, titles, complete, client.forum_fid, prune=prune)

    if new_cache == cache:
        LOG.info("关注列表无变化，未改动 %s", data)
    elif not new_cache:
        LOG.error("合并结果为空，为安全起见不写入 %s", data)
        return cache, stats
    else:
        save_cache(new_cache, path=data)
        LOG.info("已写入 %s（备份 %s）", data, BACKUP_FILE)
    log_stats(len(cache), new_cache, stats)
    return new_cache, stats


def cmd_sign(client, cache, interval=None):
    """签到所有缓存的贴吧，返回统计字典。"""
    counters = {"ok": 0, "already": 0, "failed": 0}

    tbs = client.fetch_tbs()
    if not tbs:
        counters["aborted"] = True
        return counters

    forums = [{"title": t, "fid": f} for t, f in cache.items()]
    if not forums:
        LOG.error("没有可签到的贴吧，请先运行 refresh")
        counters["aborted"] = True
        return counters

    start = time.time()
    LOG.info("共需签到 %d 个贴吧", len(forums))
    failures = []
    for forum in forums:
        res = client.sign(forum, tbs)
        code = str(res.get("error_code", ""))
        if code == "0":
            counters["ok"] += 1
        elif code == "160002":
            counters["already"] += 1
        else:
            counters["failed"] += 1
            failures.append(forum["title"])

    counters["failures"] = failures
    counters["elapsed"] = round(time.time() - start, 2)
    LOG.info("签到完成：成功 %d，已签 %d，失败 %d，用时 %.2f 秒",
             counters["ok"], counters["already"], counters["failed"],
             counters["elapsed"])
    return counters


def write_report(path, payload):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        LOG.info("运行报告已写入 %s", path)
    except OSError as e:
        LOG.error("写入报告 %s 失败：%s", path, e)


# ------------------------------------------------------------------ CLI

def build_parser():
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="贴吧全量关注列表获取 + 手机端签到",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="凭据读取顺序：环境变量 → .env 文件 → 交互式输入\n"
               "  例：export TIEBA_BDUSS=... TIEBA_STOKEN=...\n"
               "       python3 main.py refresh --no-prune",
    )
    parser.add_argument("command", nargs="?", choices=("run", "refresh", "sign"),
                        default=None,
                        help="run=更新+签到（默认），refresh=只更新列表，sign=只签到")
    parser.add_argument("--data", default=DATA_FILE, metavar="PATH",
                        help=f"缓存文件路径（默认 {DATA_FILE}）")
    parser.add_argument("--no-prune", action="store_true",
                        help="只增不删，永不自动移除贴吧")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"单次请求超时秒数（默认 {DEFAULT_TIMEOUT}）")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help=f"请求间隔秒数（默认 {DEFAULT_INTERVAL}）")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help=f"失败重试次数（默认 {DEFAULT_RETRIES}）")
    parser.add_argument("--report", metavar="PATH", default=None,
                        help="把本次运行摘要写成 JSON")
    parser.add_argument("--no-prompt", action="store_true",
                        help="缺少凭据时不要交互式询问（适合 cron）")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="输出更多日志（可重复）")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="只输出错误")

    # 兼容旧参数
    legacy = parser.add_argument_group("兼容旧参数")
    legacy.add_argument("--refresh-only", dest="legacy", action="store_const", const="refresh",
                        help="同 refresh")
    legacy.add_argument("--sign-only", dest="legacy", action="store_const", const="sign",
                        help="同 sign")
    legacy.add_argument("--refresh", dest="legacy", action="store_const", const="refresh",
                        help="同 refresh")
    legacy.add_argument("--sign", dest="legacy", action="store_const", const="sign",
                        help="同 sign")
    return parser


def setup_logging(verbose=0, quiet=False):
    if quiet:
        level = logging.ERROR
    elif verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.INFO

    fmt = "%(levelname)s: %(message)s" if verbose or quiet else "%(message)s"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt))
    LOG.handlers.clear()
    LOG.addHandler(handler)
    LOG.setLevel(level)
    LOG.propagate = False


def main(argv=None):
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.quiet)
    load_env_file()

    command = args.command or args.legacy or "run"
    need_stoken = command in ("run", "refresh")
    need_bduss = command in ("run", "sign")

    interactive = not args.no_prompt and sys.stdin.isatty()
    bduss = resolve_credential(ENV_BDUSS, "请输入 BDUSS（不回显）：", interactive) if need_bduss else ""
    stoken = resolve_credential(ENV_STOKEN, "请输入 STOKEN（不回显）：", interactive) if need_stoken else ""

    if need_bduss and not bduss:
        LOG.error("缺少 %s：可设置环境变量、写入 .env，或直接运行让脚本提示输入", ENV_BDUSS)
        return 1
    if need_stoken and not stoken:
        LOG.error("缺少 %s：刷新关注列表需要它（只签到可改用 `main.py sign`）", ENV_STOKEN)
        return 1

    try:
        cache = load_cache(args.data)
    except ValueError as e:
        LOG.error("%s", e)
        LOG.error("为避免覆盖数据已中止，请手动检查后再运行")
        return 1

    client = TiebaClient(
        bduss=bduss,
        stoken=stoken,
        timeout=args.timeout,
        interval=args.interval,
        retries=args.retries,
    )

    report = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "command": command, "data": args.data}
    exit_code = 0

    if command in ("run", "refresh"):
        cache, stats = cmd_refresh(client, args.data, prune=not args.no_prune)
        report["refresh"] = {"total": len(cache), **(stats or {}), "skipped": stats is None}
        if stats is None:
            exit_code = 1

    if command in ("run", "sign"):
        counters = cmd_sign(client, cache)
        report["sign"] = counters
        if counters.get("aborted") or counters.get("failed"):
            exit_code = 1

    if args.report:
        write_report(args.report, report)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
