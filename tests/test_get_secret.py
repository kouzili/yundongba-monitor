"""一次性取密钥工具的两块纯逻辑：候选抽取 与 响应分类。

网络那一层通过注入 probe 函数隔离，测试不打真实接口。
"""
from tools import get_secret


# ---- 候选抽取 ----

def test_literal_next_to_key_equals_is_ranked_first():
    js = {"app.js": 'var noise="aaaaaaaaaaaa";function signBymd5(t){'
                    'return md5(u+"&key="+"realsecret12345")}'}

    candidates, _ = get_secret.extract_candidates(js)

    assert candidates[0] == "realsecret12345"
    assert "aaaaaaaaaaaa" in candidates


def test_candidates_are_deduplicated_preserving_first_position():
    js = {"a.js": '"&key="+"dupsecret1234";x="dupsecret1234";y="dupsecret1234"'}

    candidates, _ = get_secret.extract_candidates(js)

    assert candidates.count("dupsecret1234") == 1


def test_literals_outside_length_bounds_are_ignored():
    js = {"a.js": '"short";"' + "x" * 65 + '";"justright12345"'}

    candidates, _ = get_secret.extract_candidates(js)

    assert "short" not in candidates
    assert "x" * 65 not in candidates
    assert "justright12345" in candidates


def test_literals_with_punctuation_are_ignored():
    js = {"a.js": '"has spaces here";"has/slash/here12";"cleanliteral1234"'}

    candidates, _ = get_secret.extract_candidates(js)

    assert candidates == ["cleanliteral1234"]


def test_limit_truncates_and_reports_how_many_were_dropped():
    js = {"a.js": ";".join(f'"candidate{i:05d}"' for i in range(50))}

    candidates, dropped = get_secret.extract_candidates(js, limit=10)

    assert len(candidates) == 10
    assert dropped == 40


def test_nothing_dropped_when_under_limit():
    js = {"a.js": '"candidate12345"'}

    candidates, dropped = get_secret.extract_candidates(js, limit=10)

    assert (candidates, dropped) == (["candidate12345"], 0)


# ---- 从缓存路径推断 appid ----

def test_appid_is_taken_from_the_containing_directory():
    # 微信桌面端的布局: .../Applet/<appid>/<版本>/__APP__.wxapkg
    path = "/x/WeChat Files/Applet/wx1234567890abcdef/137/__APP__.wxapkg"

    assert get_secret.guess_appid(path) == "wx1234567890abcdef"


def test_appid_is_found_even_when_nested_deeper():
    path = "/x/Applet/wxabcdef1234567890/12/sub/pages.wxapkg"

    assert get_secret.guess_appid(path) == "wxabcdef1234567890"


def test_returns_none_when_no_appid_looking_component_exists():
    assert get_secret.guess_appid("/home/kou/wxpkg/acct1/_123_45.wxapkg") is None


def test_does_not_mistake_short_wx_words_for_an_appid():
    # appid 是 wx + 16 位十六进制，别把随便一个以 wx 开头的目录名当成它
    assert get_secret.guess_appid("/x/wxfiles/pkg.wxapkg") is None
    assert get_secret.guess_appid("/x/wxanewfiles/a.wxapkg") is None


# ---- adb 可执行文件选择 ----

def test_explicit_adb_path_wins(tmp_path):
    bundled = tmp_path / "bundled-adb"
    bundled.write_text("")

    assert get_secret.resolve_adb("/my/adb", bundled) == "/my/adb"


def test_bundled_adb_is_preferred_over_path(tmp_path):
    # 系统源里的 adb 可能老到没有 `adb pair`，自带的这份是官方最新版
    bundled = tmp_path / "platform-tools" / "adb"
    bundled.parent.mkdir()
    bundled.write_text("")

    assert get_secret.resolve_adb(None, bundled) == str(bundled)


def test_falls_back_to_path_when_nothing_is_bundled(tmp_path):
    assert get_secret.resolve_adb(None, tmp_path / "missing") == "adb"


# ---- adb 输出解析 ----

def test_carriage_returns_from_adb_shell_are_stripped():
    # adb shell 走的是 pty，行尾是 CRLF
    out = "/sdcard/Android/data/com.tencent.mm/MicroMsg/abc/appbrand/pkg\r\n"

    assert get_secret.parse_adb_dirs(out) == \
        ["/sdcard/Android/data/com.tencent.mm/MicroMsg/abc/appbrand/pkg"]


def test_multiple_wechat_accounts_yield_multiple_dirs():
    out = ("/sdcard/Android/data/com.tencent.mm/MicroMsg/aaa/appbrand/pkg\r\n"
           "/sdcard/Android/data/com.tencent.mm/MicroMsg/bbb/appbrand/pkg\r\n")

    assert len(get_secret.parse_adb_dirs(out)) == 2


def test_shell_error_lines_are_not_mistaken_for_paths():
    out = ("ls: /sdcard/Android/data/com.tencent.mm/MicroMsg/*/appbrand/pkg: "
           "No such file or directory\r\n")

    assert get_secret.parse_adb_dirs(out) == []


def test_unexpanded_glob_is_rejected():
    # 目录不存在时某些 shell 会把通配符原样吐回来
    out = "/sdcard/Android/data/com.tencent.mm/MicroMsg/*/appbrand/pkg\r\n"

    assert get_secret.parse_adb_dirs(out) == []


def test_blank_output_yields_nothing():
    assert get_secret.parse_adb_dirs("\r\n  \r\n") == []


def test_relative_or_junk_lines_are_ignored():
    out = "daemon started successfully\r\n/sdcard/Android/data/x/appbrand/pkg\r\n"

    assert get_secret.parse_adb_dirs(out) == ["/sdcard/Android/data/x/appbrand/pkg"]


# ---- 认出哪个包是韵动吧 ----

def test_package_referencing_the_api_host_is_recognised():
    js = {"app.js": 'var base="https://wxapi.sports8.com.cn";'}

    assert get_secret.is_target_package(js) is True


def test_unrelated_mini_program_package_is_not_recognised():
    js = {"app.js": 'var base="https://api.example.com";var x="meituan"'}

    assert get_secret.is_target_package(js) is False


def test_recognition_looks_across_all_files_not_just_the_first():
    js = {"a.js": "nothing here", "b.js": 'url="wxapi.sports8.com.cn/api"'}

    assert get_secret.is_target_package(js) is True


def test_empty_package_is_not_recognised():
    assert get_secret.is_target_package({}) is False


# ---- 响应分类 ----

def test_return_code_zero_means_secret_correct_and_no_session_needed():
    verdict = get_secret.classify({"returnCode": "0", "returnData": {"fieldList": []}})

    assert verdict == get_secret.HIT_NO_SESSION


def test_signature_error_means_wrong_candidate():
    verdict = get_secret.classify({"returnCode": "-1", "returnMsg": "签名错误"})

    assert verdict == get_secret.WRONG_SECRET


def test_missing_sign_parameter_is_also_treated_as_signature_problem():
    # 不是候选对错的信号，别误判成「密钥对但要登录」
    verdict = get_secret.classify({"returnCode": "-1", "returnMsg": "参数[sign]不能为空"})

    assert verdict == get_secret.WRONG_SECRET


def test_login_error_means_secret_correct_but_session_required():
    verdict = get_secret.classify({"returnCode": "-1", "returnMsg": "用户未登录"})

    assert verdict == get_secret.HIT_NEEDS_SESSION


def test_session_expired_error_also_means_session_required():
    verdict = get_secret.classify({"returnCode": "-1", "returnMsg": "会话失效，请重新登录"})

    assert verdict == get_secret.HIT_NEEDS_SESSION


# ---- 探测循环 ----

def test_probe_stops_at_first_hit_and_reports_it():
    tried = []

    def probe(secret):
        tried.append(secret)
        return {"returnCode": "0"} if secret == "goodsecret1234" \
            else {"returnCode": "-1", "returnMsg": "签名错误"}

    result = get_secret.probe_candidates(
        ["bad1abcdefgh", "goodsecret1234", "bad2abcdefgh"], probe, pause=0)

    assert result.secret == "goodsecret1234"
    assert result.verdict == get_secret.HIT_NO_SESSION
    assert tried == ["bad1abcdefgh", "goodsecret1234"]


def test_probe_stops_on_session_required_hit_too():
    def probe(secret):
        return {"returnCode": "-1", "returnMsg": "用户未登录"}

    result = get_secret.probe_candidates(["onlycandidate1"], probe, pause=0)

    assert result.secret == "onlycandidate1"
    assert result.verdict == get_secret.HIT_NEEDS_SESSION


def test_probe_reports_no_hit_when_every_candidate_is_rejected():
    def probe(secret):
        return {"returnCode": "-1", "returnMsg": "签名错误"}

    result = get_secret.probe_candidates(["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
                                         probe, pause=0)

    assert result.secret is None
    assert result.verdict is None
    assert result.tried == 2


def test_probe_survives_a_candidate_that_raises():
    def probe(secret):
        if secret == "boomboomboom":
            raise RuntimeError("connection reset")
        return {"returnCode": "0"}

    result = get_secret.probe_candidates(["boomboomboom", "goodone12345"],
                                         probe, pause=0)

    assert result.secret == "goodone12345"


# ---- 同一 appid 下的包一荣俱荣 ----

def texts(*pairs):
    return dict(pairs)


def test_sibling_packages_of_a_matching_group_are_all_included():
    # 主包提到 API 域名，分包只有业务代码 —— 签名可能在分包里，不能丢
    entries = [
        ("wxaaa", "__APP__.wxapkg", texts(("app.js", 'h="wxapi.sports8.com.cn"'))),
        ("wxaaa", "_sub_.wxapkg", texts(("sub.js", 'k="SECRETINSUBPKG1"'))),
    ]

    merged, matched = get_secret.merge_target_groups(entries)

    assert len(matched) == 2
    assert any("SECRETINSUBPKG1" in t for t in merged.values())


def test_group_without_any_domain_reference_is_dropped():
    entries = [
        ("wxbbb", "__APP__.wxapkg", texts(("app.js", 'h="api.meituan.com"'))),
        ("wxbbb", "_sub_.wxapkg", texts(("sub.js", 'x="somethingelse1"'))),
    ]

    merged, matched = get_secret.merge_target_groups(entries)

    assert (merged, matched) == ({}, [])


def test_only_the_matching_group_survives_among_several():
    entries = [
        ("wxaaa", "a.wxapkg", texts(("a.js", 'h="wxapi.sports8.com.cn"'))),
        ("wxbbb", "b.wxapkg", texts(("b.js", 'h="api.meituan.com"'))),
        ("wxccc", "c.wxapkg", texts(("c.js", 'h="api.dianping.com"'))),
    ]

    merged, matched = get_secret.merge_target_groups(entries)

    assert matched == ["a.wxapkg"]
    assert all("meituan" not in t and "dianping" not in t for t in merged.values())


def test_same_js_filename_in_two_packages_does_not_overwrite():
    entries = [
        ("wxaaa", "main.wxapkg", texts(("app.js", 'h="wxapi.sports8.com.cn"'))),
        ("wxaaa", "sub.wxapkg", texts(("app.js", 'k="DIFFERENTCONTENT"'))),
    ]

    merged, _ = get_secret.merge_target_groups(entries)

    assert len(merged) == 2
    assert any("DIFFERENTCONTENT" in t for t in merged.values())


def test_no_entries_yields_nothing():
    assert get_secret.merge_target_groups([]) == ({}, [])


# ---- 预言机也要认两套信封 ----

def test_ydbclub_success_envelope_is_a_hit():
    verdict = get_secret.classify({"result_code": "0", "result_data": {"list": []}})

    assert verdict == get_secret.HIT_NO_SESSION


def test_ydbclub_signature_error_is_a_wrong_candidate():
    verdict = get_secret.classify({"result_code": "-1",
                                   "result_msg": "Api授权失败，请检查签名"})

    assert verdict == get_secret.WRONG_SECRET


def test_ydbclub_other_error_means_secret_ok_but_session_needed():
    verdict = get_secret.classify({"result_code": "-1", "result_msg": "请先登录"})

    assert verdict == get_secret.HIT_NEEDS_SESSION
