"""接口程序出问题时能看出来：连不上 / 返回异常 与账号问题区分

三个平台的 client._request 是接口程序回应的唯一检查点；bridge 记最近一次
异常与版本号供设置页显示；下载任务遇到接口程序问题时原因原样写入、不换账号。
接口程序的回应形状取自 2026-11-22 本地实跑三个内置二进制（无外网）的结果。
"""

import threading

import pytest
import requests

import task_manager
from core.providers import _bridge_base
from core.providers._bridge_base import ApiProgramError
from core.providers.kugou import client as kugou_client
from core.providers.kugou.bridge import KuGouApiBridge
from core.providers.netease import client as netease_client
from core.providers.netease.bridge import NodeBridge
from core.providers.qq import client as qq_client
from core.providers.qq.bridge import QqApiBridge
from models import Account, DownloadTask, db


def _resp(status: int, text: str) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = text.encode("utf-8")
    r.encoding = "utf-8"
    return r


class _Session:
    """按顺序返回预设回应；元素为异常时抛出"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def _next(self, *args, **kwargs):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    get = _next
    post = _next


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    for mod in (netease_client, qq_client, kugou_client):
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)


def _netease(tmp_path, replies):
    c = netease_client.NeteaseClient(custom_base_url="http://api.test")
    c._bridge = NodeBridge(bin_dir=tmp_path)
    c.session = _Session(replies)
    return c


def _qq(tmp_path, replies):
    c = qq_client.QqClient(custom_base_url="http://api.test")
    c._bridge = QqApiBridge(bin_dir=tmp_path)
    c.session = _Session(replies)
    return c


def _kugou(tmp_path, replies, monkeypatch):
    c = kugou_client.KuGouClient(custom_base_url="http://api.test")
    c._bridge = KuGouApiBridge(bin_dir=tmp_path)
    c.session = _Session(replies)
    monkeypatch.setattr(c, "_build_cookie", lambda: "dfid=x")
    return c


# ----------------------------------------------------------------------
# 网易云
# ----------------------------------------------------------------------
def test_netease_unreachable_after_retries(tmp_path):
    c = _netease(tmp_path, [requests.ConnectionError("refused")] * 3)
    with pytest.raises(ApiProgramError) as ei:
        c._request("/song/url/v1")
    assert ei.value.kind == "unreachable"
    assert "网易云接口程序连不上" in str(ei.value)
    assert "不是账号问题" in str(ei.value)
    assert c.session.calls == 3
    assert c._bridge.last_problem["kind"] == "unreachable"


def test_netease_missing_route_is_bad_response(tmp_path):
    # 路由不存在：Express 返回 HTML 404（实测）
    c = _netease(tmp_path, [_resp(404, "<pre>Cannot GET /song/url/v1</pre>")])
    with pytest.raises(ApiProgramError) as ei:
        c._request("/song/url/v1")
    assert ei.value.kind == "bad_response"
    assert "没有 /song/url/v1 这个接口" in str(ei.value)
    assert c.session.calls == 1


def test_netease_5xx_retried_then_bad_response(tmp_path):
    # 接口程序连不上平台：HTTP 502 + JSON（实测）
    body = '{"code":502,"msg":"getaddrinfo ENOTFOUND music.163.com"}'
    c = _netease(tmp_path, [_resp(502, body)] * 3)
    with pytest.raises(ApiProgramError) as ei:
        c._request("/login/status")
    assert ei.value.kind == "bad_response"
    assert "getaddrinfo ENOTFOUND" in str(ei.value)
    assert c.session.calls == 3


def test_netease_no_platform_reply_is_bad_response(tmp_path):
    # 接口程序没拿到平台回应：HTTP 404 + 固定兜底体（断网实测 /song/url/v1）
    c = _netease(tmp_path, [_resp(404, '{"code":404,"data":null,"msg":"Not Found"}')])
    with pytest.raises(ApiProgramError) as ei:
        c._request("/song/url/v1")
    assert "没拿到平台的回应" in str(ei.value)


def test_netease_4xx_json_returned_as_platform_result(tmp_path):
    # 其他 4xx 带 JSON：平台业务码，原样交给调用点
    c = _netease(tmp_path, [_resp(400, '{"code":400,"msg":"参数错误"}')])
    assert c._request("/song/url/v1") == {"code": 400, "msg": "参数错误"}


def test_netease_song_urls_data_not_list_is_bad_response(tmp_path):
    c = _netease(tmp_path, [_resp(200, '{"code":200,"data":{"url":"x"}}')])
    with pytest.raises(ApiProgramError) as ei:
        c.get_song_urls([1], level="exhigh")
    assert "data 不是数组" in str(ei.value)


def test_netease_song_urls_301_marked_as_account(tmp_path):
    c = _netease(tmp_path, [_resp(200, '{"code":301,"msg":"需要登录"}')])
    items = c.get_song_urls([1], level="exhigh")
    assert items[0]["err"].startswith("账号登录失效")


# ----------------------------------------------------------------------
# QQ音乐
# ----------------------------------------------------------------------
def test_qq_500_text_is_bad_response(tmp_path):
    # 接口程序内部出错：HTTP 500 纯文本（实测）
    c = _qq(tmp_path, [_resp(500, "Internal Server Error")] * 3)
    with pytest.raises(ApiProgramError) as ei:
        c._request("/song/get_cdn_dispatch")
    assert ei.value.kind == "bad_response"
    assert "QQ音乐接口程序返回异常" in str(ei.value)
    assert c.session.calls == 3


def test_qq_unreachable(tmp_path):
    c = _qq(tmp_path, [requests.ConnectionError("refused")] * 3)
    with pytest.raises(ApiProgramError) as ei:
        c._request("/song/query_song", body={})
    assert ei.value.kind == "unreachable"


def test_qq_data_not_object_is_bad_response(tmp_path):
    c = _qq(tmp_path, [_resp(200, '{"code":0,"msg":"ok","data":[1,2]}')])
    with pytest.raises(ApiProgramError) as ei:
        c._request("/song/query_song", body={})
    assert "data 不是对象" in str(ei.value)


def test_qq_platform_failures_unchanged(tmp_path):
    # 业务失败 / 401 / data 为 null 仍按原语义返回 {}
    c = _qq(tmp_path, [
        _resp(200, '{"code":104003,"msg":"no right"}'),
        _resp(401, '{"code":-1,"msg":"need login"}'),
        _resp(200, '{"code":0,"msg":"ok","data":null}'),
    ])
    assert c._request("/song/get_song_urls", body={}) == {}
    assert c._request("/song/get_song_urls", body={}) == {}
    assert c._request("/song/get_song_urls", body={}) == {}
    assert c._bridge.last_problem is None


def test_qq_account_test_reports_program_not_cookie(tmp_path):
    c = _qq(tmp_path, [_resp(500, "Internal Server Error")])
    with pytest.raises(ApiProgramError) as ei:
        c.get_user_info()
    assert "Cookie" not in str(ei.value)
    assert "QQ音乐接口程序返回异常" in str(ei.value)


# ----------------------------------------------------------------------
# 酷狗
# ----------------------------------------------------------------------
@pytest.mark.parametrize("body,needle", [
    # 接口程序访问平台出错：msg 为 JS 异常对象（两种都实测过）
    ('{"status":0,"msg":{"message":"Request failed with status code 403",'
     '"name":"AxiosError","stack":"AxiosError: ..."}}', "status code 403"),
    ('{"status":0,"msg":{"message":"getaddrinfo ENOTFOUND userservice.kugou.com",'
     '"name":"Error","stack":"Error: ..."}}', "ENOTFOUND"),
])
def test_kugou_platform_error_object_is_bad_response(tmp_path, monkeypatch, body, needle):
    c = _kugou(tmp_path, [_resp(502, body)], monkeypatch)
    with pytest.raises(ApiProgramError) as ei:
        c._request("/search", {"keywords": "a"})
    assert ei.value.kind == "bad_response"
    assert needle in str(ei.value)


def test_kugou_missing_route_is_bad_response(tmp_path, monkeypatch):
    c = _kugou(tmp_path, [_resp(404, "<pre>Cannot GET /search</pre>")], monkeypatch)
    with pytest.raises(ApiProgramError) as ei:
        c._request("/search", {"keywords": "a"})
    assert "没有 /search 这个接口" in str(ei.value)


def test_kugou_unreachable_after_retries(tmp_path, monkeypatch):
    c = _kugou(tmp_path, [requests.Timeout("timed out")] * 3, monkeypatch)
    with pytest.raises(ApiProgramError) as ei:
        c._request("/search", {"keywords": "a"})
    assert ei.value.kind == "unreachable"
    assert "酷狗接口程序连不上" in str(ei.value)


def test_kugou_business_failure_returned(tmp_path, monkeypatch):
    # 平台业务失败（HTTP 502 + 业务 JSON）仍原样返回
    c = _kugou(tmp_path, [_resp(502, '{"status":0,"error_code":20018}')], monkeypatch)
    assert c._request("/search", {"keywords": "a"}) == {"status": 0, "error_code": 20018}


# ----------------------------------------------------------------------
# bridge：启动失败、换新版目录、版本号
# ----------------------------------------------------------------------
def test_missing_binary_recorded_as_unreachable(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_DATA_DIR", raising=False)
    br = NodeBridge(bin_dir=tmp_path)
    with pytest.raises(ApiProgramError) as ei:
        br.start()
    assert ei.value.kind == "unreachable"
    st = br.status()
    assert st["last_problem"]["kind"] == "unreachable"
    assert "网易云接口程序连不上" in st["last_problem"]["text"]
    assert st["version"] is None
    assert st["bin_updated_at"] is None


def test_override_dir_preferred(tmp_path, monkeypatch):
    builtin = tmp_path / "api"
    builtin.mkdir()
    name = NodeBridge.BINARIES[_bridge_base.sys.platform]
    (builtin / name).write_text("old")
    data = tmp_path / "data"
    (data / "api").mkdir(parents=True)
    monkeypatch.setenv("APP_DATA_DIR", str(data))

    # 数据目录没有同名文件：用内置
    assert NodeBridge(bin_dir=builtin).bin_path == builtin / name
    # 放入同名文件：优先用它
    (data / "api" / name).write_text("new")
    br = NodeBridge(bin_dir=builtin)
    assert br.bin_path == (data / "api" / name).resolve()
    assert br.status()["bin_updated_at"] is not None


@pytest.mark.parametrize("cls,body,expected", [
    (NodeBridge, '{"code":200,"data":{"version":"4.40.1"}}', "4.40.1"),
    (QqApiBridge, '{"openapi":"3.1.0","info":{"title":"QQMusic API","version":"0.7.2-dev.2"}}',
     "0.7.2-dev.2"),
    (NodeBridge, '{"code":200,"data":{}}', None),
    (NodeBridge, "<html>", None),
])
def test_read_version(tmp_path, monkeypatch, cls, body, expected):
    br = cls(bin_dir=tmp_path)
    br.base_url = "http://api.test"
    monkeypatch.setattr(_bridge_base.requests, "get", lambda *a, **k: _resp(200, body))
    assert br._read_version() == expected


def test_kugou_has_no_version(tmp_path):
    assert KuGouApiBridge(bin_dir=tmp_path)._read_version() is None


# ----------------------------------------------------------------------
# 下载任务：原因原样写入，不换账号
# ----------------------------------------------------------------------
class _BrokenProvider:
    calls = 0

    def get_song_url_with_fallback(self, sid, level):
        _BrokenProvider.calls += 1
        raise ApiProgramError("网易云", "unreachable", "请求 /song/url/v1 失败（refused）", "ncm-api")


def test_task_fails_with_program_reason_without_switching(webapp_app, monkeypatch):
    (app,) = webapp_app
    db.session.add(Account(platform="netease", name="A", enabled=True))
    db.session.add(Account(platform="netease", name="B", enabled=True))
    t = DownloadTask(platform="netease", song_id="5001", song_name="测试歌曲",
                     artists="测试歌手", playlist_name="搜索单曲", status="pending")
    db.session.add(t)
    db.session.commit()
    pk = t.pk

    tm = task_manager.TaskManager(app)  # 构造不启动线程/调度器
    _BrokenProvider.calls = 0
    monkeypatch.setattr(tm, "_get_client_for_account", lambda acc: _BrokenProvider())

    # 跑一轮 worker：处理完队列里这一条就停
    tm._task_queue.put(pk)
    worker = threading.Thread(target=tm._worker_loop)
    worker.start()
    tm._task_queue.join()
    tm._stop_event.set()
    worker.join(timeout=5)

    db.session.expire_all()
    row = DownloadTask.query.filter_by(pk=pk).first()
    assert row.status == "failed"
    assert row.error_msg.startswith("网易云接口程序连不上")
    assert "处理异常" not in row.error_msg
    assert _BrokenProvider.calls == 1
