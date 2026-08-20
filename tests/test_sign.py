"""签名算法与排期解析的特征测试。

这些测试是为「删除下单路径 / 去掉 secret_ydb」这次重构准备的安全网：
它们描述的是改动前后都必须成立的行为，所以在重构前就应该是绿的。
"""
import sign


# ---- 签名算法 ----

def test_sign_matches_known_vector():
    # 规范串: 参数按 key 升序拼 "k=v&..."，再接 "&key=<secret>"，MD5 转大写
    params = {
        "stadiumid": "1128",
        "date": 1786118400,
        "userid": 0,
        "biz": "apiGetStadiumShedule",
        "method": "WxAppBooking",
        "nonce": "260720123",
    }

    assert sign.sign_bymd5(params, "testsecret") == "E6A35E7908EAFF21303FD3BE592C4737"


def test_sign_is_independent_of_dict_insertion_order():
    a = {"biz": "x", "date": 1, "nonce": "n"}
    b = {"nonce": "n", "biz": "x", "date": 1}

    assert sign.sign_bymd5(a, "s") == sign.sign_bymd5(b, "s")


def test_empty_and_none_values_are_excluded_from_signing():
    base = {"biz": "x", "nonce": "n"}
    padded = {"biz": "x", "nonce": "n", "blank": "", "missing": None}

    assert sign.sign_bymd5(padded, "s") == sign.sign_bymd5(base, "s")


def test_zero_is_signed_rather_than_treated_as_empty():
    # userid=0 是真实会出现的值，不能被当成空值剔掉
    with_zero = {"biz": "x", "userid": 0}
    without = {"biz": "x"}

    assert sign.sign_bymd5(with_zero, "s") != sign.sign_bymd5(without, "s")


def test_different_secrets_produce_different_signatures():
    params = {"biz": "x", "nonce": "n"}

    assert sign.sign_bymd5(params, "a") != sign.sign_bymd5(params, "b")


def test_nonce_carries_the_expected_prefix():
    assert sign.make_nonce().startswith(sign.NONCE_PREFIX)


# ---- 排期解析 ----

def schedule_response(entries):
    """entries: [(球场名, timePoint, status)] -> 接口返回体形状"""
    fields = {}
    for name, time_point, status in entries:
        fields.setdefault(name, []).append({
            "fieldid": f"{time_point}{name}",
            "timePoint": time_point,
            "status": status,
            "realPrice": 400,
        })
    return {
        "stadiumName": "唛恩网球中心（东馆）",
        "fieldList": [{"name": n, "shedule": s} for n, s in fields.items()],
    }


def test_slots_inside_target_window_are_returned():
    data = schedule_response([("室内08", 18, "0"), ("室内08", 19, "0")])

    parsed = sign.parse_schedule(data, 18, 21)

    assert [s["timePoint"] for s in parsed["target"]] == [18, 19]
    assert parsed["stadiumName"] == "唛恩网球中心（东馆）"


def test_booked_slots_are_not_reported_as_free():
    data = schedule_response([("室内08", 18, "1"), ("室内08", 19, "0")])

    parsed = sign.parse_schedule(data, 18, 21)

    assert [s["timePoint"] for s in parsed["target"]] == [19]


def test_slot_carries_field_name_time_and_price():
    data = schedule_response([("室内08", 18, "0")])

    slot = sign.parse_schedule(data, 18, 21)["target"][0]

    assert slot["field"] == "室内08"
    assert slot["time"] == "18:00"
    assert slot["price"] == 400
    assert slot["fieldid"] == "18室内08"


def test_nearest_reports_closest_slot_outside_the_window():
    data = schedule_response([("室内08", 10, "0"), ("室内08", 16, "0"),
                              ("室内08", 23, "0")])

    parsed = sign.parse_schedule(data, 18, 21)

    assert parsed["target"] == []
    assert parsed["nearest"]["timePoint"] == 16   # 距 18 点 2 小时，比 23 点更近


def test_nearest_is_none_when_target_window_has_slots():
    data = schedule_response([("室内08", 16, "0"), ("室内08", 19, "0")])

    parsed = sign.parse_schedule(data, 18, 21)

    assert parsed["nearest"] is None


def test_fully_booked_day_reports_no_target_and_no_nearest():
    data = schedule_response([("室内08", 18, "1"), ("室内08", 19, "2")])

    parsed = sign.parse_schedule(data, 18, 21)

    assert parsed["target"] == []
    assert parsed["nearest"] is None
    assert parsed["all_count"] == 0


def test_error_response_is_passed_through_untouched():
    parsed = sign.parse_schedule({"error": "签名错误"}, 18, 21)

    assert parsed == {"error": "签名错误"}


def test_slots_with_missing_time_point_are_skipped():
    data = {"stadiumName": "x", "fieldList": [{"name": "f", "shedule": [
        {"fieldid": "1", "timePoint": None, "status": "0", "realPrice": 1}]}]}

    parsed = sign.parse_schedule(data, 18, 21)

    assert parsed["all_count"] == 0


# ---- 日期换算 ----

def test_date_to_ts_returns_local_midnight():
    import datetime

    ts = sign.date_to_ts("2026-08-15")

    assert datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") \
        == "2026-08-15 00:00:00"


# ---- 资源管理 ----

def test_reload_secrets_closes_the_config_file():
    """json.load(open(...)) 不关文件会触发 ResourceWarning。

    在 CPython 上引用计数会立刻回收，不算真泄漏，但：
      · 打开 filterwarnings=error 后整个测试套件在干净环境里会崩
      · 换非引用计数的实现（PyPy）就是真泄漏
    而这个函数在长跑进程里会被反复调用。
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        sign.reload_secrets()


# ---- 按接口前缀选密钥 ----

def test_api_prefix_uses_the_api_secret():
    assert sign.secret_for("/api/ydb/stadium/apiGetStadiumShedule") is sign.SECRET_API


def test_ydb_prefix_uses_the_ydb_secret():
    # 畅打走 YDBCLUB/，支付走 YDB/ —— 都不是 /api/，用另一把密钥
    assert sign.secret_for("/YDB/weapp/Wxapp/getShortLink") is sign.SECRET_YDB
    assert sign.secret_for("/YDBCLUB/service/personalCenter/activityList") is sign.SECRET_YDB


def test_prefix_match_is_case_sensitive_like_the_real_paths():
    # 真实路径大小写是固定的，别把 /API/ 当成 /api/
    assert sign.secret_for("/API/x") is sign.SECRET_YDB


# ---- 两套返回信封归一化 ----

def test_api_envelope_success_returns_the_payload():
    data = {"returnCode": "0", "returnMsg": None, "returnData": {"fieldList": [1]}}

    assert sign.normalize_response(data) == {"fieldList": [1]}


def test_api_envelope_error_becomes_an_error_dict():
    data = {"returnCode": "-1", "returnMsg": "签名错误", "returnData": None}

    assert sign.normalize_response(data) == {"error": "签名错误"}


def test_ydbclub_envelope_success_returns_the_payload():
    # YDBCLUB/ 用 result_code / result_msg / result_data，字段名完全不同
    data = {"result_code": "0", "result_msg": None, "result_data": {"list": [1]}}

    assert sign.normalize_response(data) == {"list": [1]}


def test_ydbclub_envelope_error_becomes_an_error_dict():
    data = {"result_code": "-1", "result_msg": "Api授权失败，请检查签名",
            "result_data": None}

    assert sign.normalize_response(data) == {"error": "Api授权失败，请检查签名"}


def test_success_with_null_payload_yields_empty_dict():
    assert sign.normalize_response({"returnCode": "0", "returnData": None}) == {}


def test_unrecognised_envelope_is_reported_as_an_error():
    result = sign.normalize_response({"weird": True})

    assert "error" in result


def test_error_without_a_message_still_reports_the_code():
    result = sign.normalize_response({"returnCode": "-9", "returnMsg": None})

    assert "-9" in result["error"]


# ---- 登录：密码哈希 ----

def test_password_is_md5_hashed_as_lowercase_hex():
    """小程序发的是 MD5(明文).toString()，CryptoJS 默认小写十六进制。

    发明文会登录失败；发大写（签名用的那种）也会失败。
    """
    assert sign.hash_password("testpass123") == "cd8ae748d23722682cc20ad62e7cb6e9"


def test_password_hash_is_lowercase_not_uppercase():
    assert sign.hash_password("testpass123").islower()


# ---- 下单参数 ----

def test_order_list_has_the_minimal_shape_the_client_sends():
    import json

    # 客户端只发 fieldid / startTime / endTime，其余字段服务端自己取
    assert json.loads(sign.build_order_list(3269, 6, 1)) == \
        [{"fieldid": 3269, "startTime": 6, "endTime": 7}]


def test_multi_hour_booking_extends_the_end_time():
    import json

    assert json.loads(sign.build_order_list(3269, 19, 2))[0]["endTime"] == 21


def test_order_list_is_a_json_string_not_a_list():
    # 外层参数要的是字符串化的 JSON
    assert isinstance(sign.build_order_list(1, 6, 1), str)


def test_zero_or_negative_hours_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        sign.build_order_list(1, 6, 0)
    with pytest.raises(ValueError):
        sign.build_order_list(1, 6, -1)


# ---- v2 搜索结果解析 ----
# 这个接口给的是「符合时段/距离/价格条件的候选场馆」，不含具体空场时段，
# 所以解析结果要能直接喂给 slots 去确认。

def raw_stadium(**over):
    base = {
        "stadiumid": 858,
        "stadiumName": "SPINTONIC网球发球机馆",
        "countyName": "静安区",
        "distance": "1203",
        "showDistance": "1.2公里",
        "minPrice": 120.0,
        "indoor": "0",
        "outdoor": "1",
        "newTags": ["室内", "学练馆"],
        "maxday": 5,
        "stadiumAddress": "上海市静安区新闸路688号",
        "stadiumTel": "19539466310",
    }
    base.update(over)
    return base


def test_stadium_result_core_fields():
    s = sign.parse_stadium_result(raw_stadium())

    assert s["stadiumid"] == "858"
    assert s["name"] == "SPINTONIC网球发球机馆"
    assert s["district"] == "静安区"
    assert s["min_price"] == 120.0
    assert s["tags"] == ["室内", "学练馆"]


def test_distance_is_converted_to_kilometres():
    # 接口给的是米
    s = sign.parse_stadium_result(raw_stadium(distance="1203"))

    assert s["distance_km"] == 1.2


def test_missing_distance_is_none_rather_than_zero():
    s = sign.parse_stadium_result(raw_stadium(distance=None))

    assert s["distance_km"] is None


def test_release_window_is_exposed():
    # maxday 是这个场馆提前放场的天数 —— 查更远的日期必然是空的
    s = sign.parse_stadium_result(raw_stadium(maxday=14))

    assert s["max_days_ahead"] == 14


def test_stadiumid_is_always_a_string_for_downstream_use():
    s = sign.parse_stadium_result(raw_stadium(stadiumid=1189))

    assert s["stadiumid"] == "1189"


def test_time_period_is_formatted_as_the_client_sends_it():
    assert sign.time_period(19, 21) == "19,21"
    assert sign.time_period(6, 23) == "6,23"


def test_time_period_defaults_to_the_whole_day():
    assert sign.time_period(None, None) == "0,24"
