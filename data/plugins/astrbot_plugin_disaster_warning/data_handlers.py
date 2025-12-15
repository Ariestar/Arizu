"""
数据源处理器架构
根据重构需求，分别处理EEW预警和地震情报
"""

import json
import re
import time
import traceback
from datetime import datetime
from typing import Any

from astrbot.api import logger

from .data_source_config import (
    get_data_source_config,
)
from .models import (
    DataSource,
    DisasterEvent,
    DisasterType,
    EarthquakeData,
    TsunamiData,
    WeatherAlarmData,
)


class BaseDataHandler:
    """基础数据处理器 - 重构版本"""

    def __init__(self, source_id: str, message_logger=None):
        self.source_id = source_id
        self.source_config = get_data_source_config(source_id)
        self.message_logger = message_logger
        # 添加心跳包检测缓存
        self._last_heartbeat_check = {}
        self._heartbeat_patterns = {
            "empty_coordinates": {"latitude": 0, "longitude": 0},
            "empty_fields": ["", None, {}],
        }
        # 添加重复警告检测缓存
        self._warning_cache = {}
        self._warning_cache_timeout = 3600  # 1小时内不重复相同的警告

    def parse_message(self, message: str) -> DisasterEvent | None:
        """解析消息 - 基础方法"""
        # 仅使用AstrBot logger进行调试日志，不再重复记录到消息记录器
        # WebSocket管理器已经记录了原始消息，包含更详细的连接信息
        logger.debug(f"[{self.source_id}] 收到原始消息，长度: {len(message)}")

        try:
            data = json.loads(message)
            return self._parse_data(data)
        except json.JSONDecodeError as e:
            logger.error(f"[灾害预警] {self.source_id} JSON解析失败: {e}")
            return None
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 消息处理失败: {e}")
            logger.error(f"[灾害预警] 异常堆栈: {traceback.format_exc()}")
            return None

    def _is_heartbeat_message(self, msg_data: dict[str, Any]) -> bool:
        """检测是否为心跳包或无效数据，msg_data 是提取后的实际数据。"""

        current_time = time.time()
        cache_key = f"{self.source_id}_last_check"

        # 检查是否在短时间内重复检测
        if cache_key in self._last_heartbeat_check:
            if (
                current_time - self._last_heartbeat_check[cache_key] < 30
            ):  # 30秒内不重复检测
                return False

        self._last_heartbeat_check[cache_key] = current_time

        # 检测空坐标数据
        if "latitude" in msg_data and "longitude" in msg_data:
            lat = msg_data.get("latitude")
            lon = msg_data.get("longitude")
            if lat == 0 and lon == 0:
                logger.debug(
                    f"[灾害预警] {self.source_id} 检测到空坐标心跳包，静默过滤"
                )
                return True

        # 检测缺少关键字段的空数据
        critical_fields = {
            "usgs_fanstudio": ["id", "magnitude", "placeName"],
            "china_tsunami_fanstudio": ["warningInfo", "title", "level"],
            "china_weather_fanstudio": ["headline", "description"],
        }

        if self.source_id in critical_fields:
            required_fields = critical_fields[self.source_id]
            missing_count = 0

            for field in required_fields:
                field_value = msg_data.get(field)
                if field_value in self._heartbeat_patterns["empty_fields"]:
                    missing_count += 1

            # 如果超过一半的关键字段为空，认为是心跳包
            if missing_count >= len(required_fields) / 2:
                logger.debug(
                    f"[灾害预警] {self.source_id} 检测到空数据心跳包，静默过滤"
                )
                return True

        return False

    def _should_log_warning(self, warning_type: str, message: str) -> bool:
        """判断是否应该记录警告（避免重复警告）"""

        current_time = time.time()
        cache_key = f"{self.source_id}_{warning_type}"

        if cache_key in self._warning_cache:
            last_time, last_message = self._warning_cache[cache_key]
            # 如果在缓存时间内且消息相同，不记录
            if (
                current_time - last_time < self._warning_cache_timeout
                and last_message == message
            ):
                return False

        # 更新缓存
        self._warning_cache[cache_key] = (current_time, message)
        return True

    def _parse_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析数据 - 子类实现"""
        raise NotImplementedError

    def _parse_datetime(self, time_str: str) -> datetime | None:
        """解析时间字符串"""
        if not time_str or not isinstance(time_str, str):
            return None

        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y/%m/%d %H:%M:%S.%f",
            "%Y/%m/%d %H:%M",
            "%Y-%m-%d %H:%M",
        ]

        for fmt in formats:
            try:
                return datetime.strptime(time_str.strip(), fmt)
            except ValueError:
                continue

        logger.warning(f"[灾害预警] 时间解析失败，返回None: '{time_str}'")
        return None


class CEAEEWHandler(BaseDataHandler):
    """中国地震预警网处理器 - FAN Studio"""

    def __init__(self, message_logger=None):
        super().__init__("cea_fanstudio", message_logger)

    def _parse_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析中国地震预警网数据"""
        try:
            # 获取实际数据 - FAN Studio使用大写D的Data字段，如果没有则使用整个数据
            msg_data = data.get("Data", {}) or data.get("data", {}) or data
            if not msg_data:
                logger.warning(f"[灾害预警] {self.source_id} 消息中没有有效数据")
                return None

            # 记录数据获取情况用于调试
            if "Data" in data:
                logger.debug(f"[灾害预警] {self.source_id} 使用Data字段获取数据")
            elif "data" in data:
                logger.debug(f"[灾害预警] {self.source_id} 使用data字段获取数据")
            else:
                logger.debug(f"[灾害预警] {self.source_id} 使用整个消息作为数据")

            # 检查是否为地震预警数据
            if "epiIntensity" not in msg_data:
                logger.debug(f"[灾害预警] {self.source_id} 非地震预警数据，跳过")
                return None

            earthquake = EarthquakeData(
                id=msg_data.get("id", ""),
                event_id=msg_data.get("eventId", ""),
                source=DataSource.FAN_STUDIO_CEA,
                disaster_type=DisasterType.EARTHQUAKE_WARNING,
                shock_time=self._parse_datetime(msg_data.get("shockTime", "")),
                latitude=float(msg_data.get("latitude", 0)),
                longitude=float(msg_data.get("longitude", 0)),
                depth=msg_data.get("depth"),
                magnitude=msg_data.get("magnitude"),
                intensity=msg_data.get("epiIntensity"),
                place_name=msg_data.get("placeName", ""),
                province=msg_data.get("province"),
                updates=msg_data.get("updates", 1),
                is_final=msg_data.get("isFinal", False),
                raw_data=msg_data,
            )

            logger.info(
                f"[灾害预警] 地震预警解析成功: {earthquake.place_name} (M {earthquake.magnitude}), 时间: {earthquake.shock_time}"
            )

            return DisasterEvent(
                id=earthquake.id,
                data=earthquake,
                source=earthquake.source,
                disaster_type=earthquake.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析数据失败: {e}")
            return None


class CEAEEWWolfxHandler(BaseDataHandler):
    """中国地震预警网处理器 - Wolfx"""

    def __init__(self, message_logger=None):
        super().__init__("cea_wolfx", message_logger)

    def _parse_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析Wolfx中国地震预警数据"""
        try:
            # 检查消息类型
            if data.get("type") != "cenc_eew":
                logger.debug(f"[灾害预警] {self.source_id} 非CENC EEW数据，跳过")
                return None

            earthquake = EarthquakeData(
                id=data.get("ID", ""),
                event_id=data.get("EventID", ""),
                source=DataSource.WOLFX_CENC_EEW,
                disaster_type=DisasterType.EARTHQUAKE_WARNING,
                shock_time=self._parse_datetime(data.get("OriginTime", "")),
                latitude=data.get("Latitude", 0),
                longitude=data.get("Longitude", 0),
                depth=data.get("Depth"),
                magnitude=data.get("Magnitude"),
                intensity=data.get("MaxIntensity"),
                place_name=data.get("HypoCenter", ""),
                updates=data.get("ReportNum", 1),
                is_final=data.get("isFinal", False),
                raw_data=data,
            )

            logger.info(
                f"[灾害预警] 地震预警解析成功: {earthquake.place_name} (M {earthquake.magnitude}), 时间: {earthquake.shock_time}"
            )

            return DisasterEvent(
                id=earthquake.id,
                data=earthquake,
                source=earthquake.source,
                disaster_type=earthquake.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析数据失败: {e}")
            return None


class CWAEEWHandler(BaseDataHandler):
    """台湾中央气象署地震预警处理器 - FAN Studio"""

    def __init__(self, message_logger=None):
        super().__init__("cwa_fanstudio", message_logger)

    def _parse_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析台湾中央气象署地震预警数据"""
        try:
            # 获取实际数据 - 兼容多种格式
            msg_data = data.get("Data", {}) or data.get("data", {}) or data
            if not msg_data:
                logger.warning(f"[灾害预警] {self.source_id} 消息中没有有效数据")
                return None

            # 记录数据获取情况用于调试
            if "Data" in data:
                logger.debug(f"[灾害预警] {self.source_id} 使用Data字段获取数据")
            elif "data" in data:
                logger.debug(f"[灾害预警] {self.source_id} 使用data字段获取数据")
            else:
                logger.debug(f"[灾害预警] {self.source_id} 使用整个消息作为数据")

            # 检查是否为CWA地震预警数据
            if "maxIntensity" not in msg_data or "createTime" not in msg_data:
                logger.debug(f"[灾害预警] {self.source_id} 非CWA地震预警数据，跳过")
                return None

            earthquake = EarthquakeData(
                id=str(msg_data.get("id", "")),
                event_id=msg_data.get("eventId", ""),
                source=DataSource.FAN_STUDIO_CWA,
                disaster_type=DisasterType.EARTHQUAKE_WARNING,
                shock_time=self._parse_datetime(msg_data.get("shockTime", "")),
                create_time=self._parse_datetime(msg_data.get("createTime", "")),
                latitude=float(msg_data.get("latitude", 0)),
                longitude=float(msg_data.get("longitude", 0)),
                depth=msg_data.get("depth"),
                magnitude=msg_data.get("magnitude"),
                scale=_safe_float_convert(msg_data.get("maxIntensity")),
                place_name=msg_data.get("placeName", ""),
                updates=msg_data.get("updates", 1),
                is_final=msg_data.get("isFinal", False),
                raw_data=msg_data,
            )

            logger.info(
                f"[灾害预警] 地震预警解析成功: {earthquake.place_name} (M {earthquake.magnitude}), 时间: {earthquake.shock_time}"
            )

            return DisasterEvent(
                id=earthquake.id,
                data=earthquake,
                source=earthquake.source,
                disaster_type=earthquake.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析数据失败: {e}")
            return None


class CWAEEWWolfxHandler(BaseDataHandler):
    """台湾中央气象署地震预警处理器 - Wolfx"""

    def __init__(self, message_logger=None):
        super().__init__("cwa_wolfx", message_logger)

    def _parse_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析Wolfx台湾地震预警数据"""
        try:
            # 检查消息类型
            if data.get("type") != "cwa_eew":
                logger.debug(f"[灾害预警] {self.source_id} 非CWA EEW数据，跳过")
                return None

            earthquake = EarthquakeData(
                id=str(data.get("ID", "")),
                event_id=data.get("EventID", ""),
                source=DataSource.WOLFX_CWA_EEW,
                disaster_type=DisasterType.EARTHQUAKE_WARNING,
                shock_time=self._parse_datetime(data.get("OriginTime", "")),
                latitude=data.get("Latitude", 0),
                longitude=data.get("Longitude", 0),
                depth=data.get("Depth"),
                magnitude=data.get("Magunitude") or data.get("Magnitude"),
                scale=self._parse_cwa_scale(data.get("MaxIntensity", "")),
                place_name=data.get("HypoCenter", ""),
                updates=data.get("ReportNum", 1),
                is_final=data.get("isFinal", False),
                raw_data=data,
            )

            logger.info(
                f"[灾害预警] 地震预警解析成功: {earthquake.place_name} (M {earthquake.magnitude}), 时间: {earthquake.shock_time}"
            )

            return DisasterEvent(
                id=earthquake.id,
                data=earthquake,
                source=earthquake.source,
                disaster_type=earthquake.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析数据失败: {e}")
            return None

    def _parse_cwa_scale(self, scale_str: str) -> float | None:
        """解析台湾震度"""
        if not scale_str:
            return None

        match = re.search(r"(\d+)(弱|強)?", scale_str)
        if match:
            base = int(match.group(1))
            suffix = match.group(2)

            if suffix == "弱":
                return base - 0.5
            elif suffix == "強":
                return base + 0.5
            else:
                return float(base)

        return None


class JMAEEWP2PHandler(BaseDataHandler):
    """日本气象厅紧急地震速报处理器 - P2P"""

    def __init__(self, message_logger=None):
        super().__init__("jma_p2p", message_logger)

    def parse_message(self, message: str) -> DisasterEvent | None:
        """解析P2P消息"""
        # 不再重复记录原始消息，WebSocket管理器已记录详细信息
        try:
            data = json.loads(message)

            # 根据code判断消息类型
            code = data.get("code")

            if code == 556:  # 緊急地震速報（警報）
                logger.debug(f"[灾害预警] {self.source_id} 收到緊急地震速報（警報）")
                return self._parse_eew_data(data)
            elif code == 554:  # 緊急地震速報 発表検出
                logger.debug(
                    f"[灾害预警] {self.source_id} 收到緊急地震速報発表検出，忽略"
                )
                return None
            else:
                logger.debug(f"[灾害预警] {self.source_id} 非EEW数据，code: {code}")
                return None

        except json.JSONDecodeError as e:
            logger.error(f"[灾害预警] {self.source_id} JSON解析失败: {e}")
            return None
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 消息处理失败: {e}")
            return None

    def _parse_eew_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析緊急地震速報数据"""
        try:
            earthquake_info = data.get("earthquake", {})
            hypocenter = earthquake_info.get("hypocenter", {})
            issue_info = data.get("issue", {})
            areas = data.get("areas", [])

            # 兼容性处理：优先检查maxScale字段
            max_scale_raw = -1
            if "maxScale" in earthquake_info:
                max_scale_raw = earthquake_info.get("maxScale", -1)
            elif "max_scale" in earthquake_info:
                max_scale_raw = earthquake_info.get("max_scale", -1)
            else:
                # 从areas中计算最大震度作为后备
                # P2P API中可能是scaleFrom或scaleTo，两者都尝试
                raw_scales = []
                for area in areas:
                    scale = area.get("scaleFrom", 0)
                    if scale <= 0:
                        scale = area.get("scaleTo", 0)
                    if scale > 0:
                        raw_scales.append(scale)

                max_scale_raw = max(raw_scales) if raw_scales else -1
                if max_scale_raw > 0:
                    logger.warning(
                        f"[灾害预警] {self.source_id} 使用areas计算maxScale: {max_scale_raw}"
                    )

            scale = (
                self._convert_p2p_scale_to_standard(max_scale_raw)
                if max_scale_raw != -1
                else None
            )

            # 兼容性处理：优先检查time字段
            shock_time = None
            if "time" in earthquake_info:
                shock_time = self._parse_datetime(earthquake_info.get("time", ""))
            elif "originTime" in earthquake_info:
                shock_time = self._parse_datetime(earthquake_info.get("originTime", ""))
            else:
                logger.warning(f"[灾害预警] {self.source_id} 缺少地震时间信息")

            # 必填字段验证 - 记录warning但继续处理
            required_hypocenter_fields = ["latitude", "longitude", "name"]
            missing_fields = []
            for field in required_hypocenter_fields:
                if field not in hypocenter or hypocenter[field] is None:
                    missing_fields.append(field)

            if missing_fields:
                logger.warning(
                    f"[灾害预警] {self.source_id} 缺少震源必填字段: {missing_fields}，继续处理..."
                )

            # 检查cancelled字段
            is_cancelled = data.get("cancelled", False)
            if is_cancelled:
                logger.info(f"[灾害预警] {self.source_id} 收到取消的EEW事件")

            # 检查test字段
            is_test = data.get("test", False)
            if is_test:
                logger.info(f"[灾害预警] {self.source_id} 收到测试模式的EEW事件")

            earthquake = EarthquakeData(
                id=data.get("id", ""),
                event_id=issue_info.get("eventId", ""),
                source=DataSource.P2P_EEW,
                disaster_type=DisasterType.EARTHQUAKE_WARNING,
                shock_time=shock_time,
                latitude=hypocenter.get("latitude", 0),
                longitude=hypocenter.get("longitude", 0),
                depth=hypocenter.get("depth"),
                magnitude=hypocenter.get("magnitude"),
                place_name=hypocenter.get("name", "未知地点"),
                scale=scale,
                is_final=data.get("is_final", False),
                is_cancel=is_cancelled,
                is_training=is_test,
                serial=issue_info.get("serial", ""),
                raw_data=data,
            )

            logger.info(
                f"[灾害预警] 地震预警解析成功: {earthquake.place_name} (M {earthquake.magnitude}), 时间: {earthquake.shock_time}"
            )

            return DisasterEvent(
                id=earthquake.id,
                data=earthquake,
                source=earthquake.source,
                disaster_type=earthquake.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析EEW数据失败: {e}")
            return None

    def _convert_p2p_scale_to_standard(self, p2p_scale: int) -> float | None:
        """将P2P震度值转换为标准震度"""
        scale_mapping = {
            10: 1.0,  # 震度1
            20: 2.0,  # 震度2
            30: 3.0,  # 震度3
            40: 4.0,  # 震度4
            45: 4.5,  # 震度5弱
            50: 5.0,  # 震度5強
            55: 5.5,  # 震度6弱
            60: 6.0,  # 震度6強
            70: 7.0,  # 震度7
        }
        return scale_mapping.get(p2p_scale)


class JMAEEWWolfxHandler(BaseDataHandler):
    """日本气象厅紧急地震速报处理器 - Wolfx"""

    def __init__(self, message_logger=None):
        super().__init__("jma_wolfx", message_logger)

    def _parse_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析Wolfx JMA EEW数据"""
        try:
            # 检查消息类型
            if data.get("type") != "jma_eew":
                logger.debug(f"[灾害预警] {self.source_id} 非JMA EEW数据，跳过")
                return None

            earthquake = EarthquakeData(
                id=data.get("EventID", ""),
                event_id=data.get("EventID", ""),
                source=DataSource.WOLFX_JMA_EEW,
                disaster_type=DisasterType.EARTHQUAKE_WARNING,
                shock_time=self._parse_datetime(data.get("OriginTime", "")),
                latitude=data.get("Latitude", 0),
                longitude=data.get("Longitude", 0),
                depth=data.get("Depth"),
                magnitude=data.get("Magunitude") or data.get("Magnitude"),
                place_name=data.get("Hypocenter", ""),
                scale=self._parse_jma_scale(data.get("MaxIntensity", "")),
                is_final=data.get("isFinal", False),
                is_cancel=data.get("isCancel", False),
                is_training=data.get("isTraining", False),
                raw_data=data,
            )

            logger.info(
                f"[灾害预警] 地震预警解析成功: {earthquake.place_name} (M {earthquake.magnitude}), 时间: {earthquake.shock_time}"
            )

            return DisasterEvent(
                id=earthquake.id,
                data=earthquake,
                source=earthquake.source,
                disaster_type=earthquake.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析数据失败: {e}")
            return None

    def _parse_jma_scale(self, scale_str: str) -> float | None:
        """解析日本震度"""
        if not scale_str:
            return None

        match = re.search(r"(\d+)(弱|強)?", scale_str)
        if match:
            base = int(match.group(1))
            suffix = match.group(2)

            if suffix == "弱":
                return base - 0.5
            elif suffix == "強":
                return base + 0.5
            else:
                return float(base)

        return None


class GlobalQuakeHandler(BaseDataHandler):
    """Global Quake处理器"""

    def __init__(self, message_logger=None):
        super().__init__("global_quake", message_logger)

    def parse_message(self, message: str) -> DisasterEvent | None:
        """解析Global Quake消息"""
        # Global Quake使用TCP连接，WebSocket管理器不会记录其消息
        # 但GlobalQuakeClient已经在websocket_manager.py第513-525行记录了TCP消息
        # 所以这里不再需要重复记录

        try:
            # Global Quake的消息格式需要根据实际情况调整
            data = json.loads(message)
            return self._parse_earthquake_data(data)
        except json.JSONDecodeError:
            # 如果不是JSON，尝试其他格式
            return self._parse_text_message(message)
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 消息处理失败: {e}")
            return None

    def _parse_earthquake_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析地震数据"""
        try:
            earthquake = EarthquakeData(
                id=data.get("id", ""),
                event_id=data.get("event_id", ""),
                source=DataSource.GLOBAL_QUAKE,
                disaster_type=DisasterType.EARTHQUAKE,
                shock_time=self._parse_datetime(data.get("time", "")),
                latitude=data.get("latitude", 0),
                longitude=data.get("longitude", 0),
                depth=data.get("depth"),
                magnitude=data.get("magnitude"),
                intensity=data.get("intensity"),
                place_name=data.get("location", ""),
                updates=data.get("revision", 1),  # 测试：使用revision作为报数
                raw_data=data,
            )

            logger.info(
                f"[灾害预警] 地震数据解析成功: {earthquake.place_name} (M {earthquake.magnitude}), 时间: {earthquake.shock_time}"
            )

            return DisasterEvent(
                id=earthquake.id,
                data=earthquake,
                source=earthquake.source,
                disaster_type=earthquake.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析地震数据失败: {e}")
            return None

    def _parse_text_message(self, message: str) -> DisasterEvent | None:
        """解析文本消息"""
        logger.debug(f"[灾害预警] {self.source_id} 文本消息: {message}")
        return None


# 地震情报处理器
class CENCEarthquakeHandler(BaseDataHandler):
    """中国地震台网地震测定处理器 - FAN Studio"""

    def __init__(self, message_logger=None):
        super().__init__("cenc_fanstudio", message_logger)

    def _parse_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析中国地震台网数据"""
        try:
            # 获取实际数据 - 兼容多种格式
            msg_data = data.get("Data", {}) or data.get("data", {}) or data
            if not msg_data:
                logger.warning(f"[灾害预警] {self.source_id} 消息中没有有效数据")
                return None

            # 记录数据获取情况用于调试
            if "Data" in data:
                logger.debug(f"[灾害预警] {self.source_id} 使用Data字段获取数据")
            elif "data" in data:
                logger.debug(f"[灾害预警] {self.source_id} 使用data字段获取数据")
            else:
                logger.debug(f"[灾害预警] {self.source_id} 使用整个消息作为数据")

            # 检查是否为CENC地震测定数据
            if "infoTypeName" not in msg_data or "eventId" not in msg_data:
                logger.debug(f"[灾害预警] {self.source_id} 非CENC地震测定数据，跳过")
                return None

            # 优化USGS数据精度 - 四舍五入到1位小数
            magnitude = msg_data.get("magnitude")
            if magnitude is not None:
                magnitude = round(float(magnitude), 1)

            depth = msg_data.get("depth")
            if depth is not None:
                depth = round(float(depth), 1)

            earthquake = EarthquakeData(
                id=str(msg_data.get("id", "")),
                event_id=msg_data.get("eventId", ""),
                source=DataSource.FAN_STUDIO_CENC,
                disaster_type=DisasterType.EARTHQUAKE,
                shock_time=self._parse_datetime(msg_data.get("shockTime", "")),
                latitude=float(msg_data.get("latitude", 0)),
                longitude=float(msg_data.get("longitude", 0)),
                depth=depth,
                magnitude=magnitude,
                place_name=msg_data.get("placeName", ""),
                info_type=msg_data.get("infoTypeName", ""),
                raw_data=msg_data,
            )

            logger.info(
                f"[灾害预警] 地震数据解析成功: {earthquake.place_name} (M {earthquake.magnitude}), 时间: {earthquake.shock_time}"
            )

            return DisasterEvent(
                id=earthquake.id,
                data=earthquake,
                source=earthquake.source,
                disaster_type=earthquake.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析数据失败: {e}")
            return None


class CENCEarthquakeWolfxHandler(BaseDataHandler):
    """中国地震台网地震测定处理器 - Wolfx"""

    def __init__(self, message_logger=None):
        super().__init__("cenc_wolfx", message_logger)

    def _parse_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析Wolfx中国地震台网地震列表"""
        try:
            # 检查消息类型
            if data.get("type") != "cenc_eqlist":
                logger.debug(f"[灾害预警] {self.source_id} 非CENC地震列表数据，跳过")
                return None

            # 只处理最新的地震
            eq_info = None
            for key, value in data.items():
                if key.startswith("No") and isinstance(value, dict):
                    eq_info = value
                    break

            if not eq_info:
                return None

            earthquake = EarthquakeData(
                id=eq_info.get("md5", ""),
                event_id=eq_info.get("md5", ""),
                source=DataSource.WOLFX_CENC_EEW,
                disaster_type=DisasterType.EARTHQUAKE,
                shock_time=self._parse_datetime(eq_info.get("time", "")),
                latitude=float(eq_info.get("latitude", 0)),
                longitude=float(eq_info.get("longitude", 0)),
                depth=float(eq_info.get("depth", 0)) if eq_info.get("depth") else None,
                magnitude=float(eq_info.get("magnitude", 0))
                if eq_info.get("magnitude")
                else None,
                intensity=float(eq_info.get("intensity", 0))
                if eq_info.get("intensity")
                else None,
                place_name=eq_info.get("location", ""),
                info_type=eq_info.get("type", ""),
                raw_data=data,
            )

            logger.info(
                f"[灾害预警] 地震数据解析成功: {earthquake.place_name} (M {earthquake.magnitude}), 时间: {earthquake.shock_time}"
            )

            return DisasterEvent(
                id=earthquake.id,
                data=earthquake,
                source=earthquake.source,
                disaster_type=earthquake.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析数据失败: {e}")
            return None


class JMAEarthquakeP2PHandler(BaseDataHandler):
    """日本气象厅地震情报处理器 - P2P"""

    def __init__(self, message_logger=None):
        super().__init__("jma_p2p_info", message_logger)

    def parse_message(self, message: str) -> DisasterEvent | None:
        """解析P2P地震情報"""
        # 不再重复记录原始消息，WebSocket管理器已记录详细信息
        try:
            data = json.loads(message)

            # 根据code判断消息类型
            code = data.get("code")

            if code == 551:  # 地震情報
                logger.debug(f"[灾害预警] {self.source_id} 收到地震情報(code:551)")
                return self._parse_earthquake_data(data)
            else:
                logger.debug(
                    f"[灾害预警] {self.source_id} 非地震情報数据，code: {code}"
                )
                return None

        except json.JSONDecodeError as e:
            logger.error(f"[灾害预警] {self.source_id} JSON解析失败: {e}")
            return None
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 消息处理失败: {e}")
            return None

    def _safe_float_convert(self, value) -> float | None:
        """安全地将值转换为浮点数 - 为JMAEarthquakeP2PHandler提供此方法"""
        return _safe_float_convert(value)

    def _parse_earthquake_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析地震情報"""
        try:
            # 获取基础数据 - 使用英文键名（实际数据格式）
            earthquake_info = data.get("earthquake", {})
            hypocenter = earthquake_info.get("hypocenter", {})
            # issue_info = data.get("issue", {})  # 未使用，注释掉以避免未使用变量警告

            # 关键字段检查
            magnitude_raw = hypocenter.get("magnitude")
            place_name = hypocenter.get("name")
            latitude = hypocenter.get("latitude")
            longitude = hypocenter.get("longitude")

            # 震级解析
            magnitude = self._safe_float_convert(magnitude_raw)
            if magnitude is None:
                logger.error(
                    f"[灾害预警] {self.source_id} 震级解析失败: {magnitude_raw}"
                )
                return None

            # 经纬度解析
            lat = self._safe_float_convert(latitude)
            lon = self._safe_float_convert(longitude)
            if lat is None or lon is None:
                logger.error(
                    f"[灾害预警] {self.source_id} 经纬度解析失败: lat={latitude}, lon={longitude}"
                )
                return None

            # 震度转换
            max_scale_raw = earthquake_info.get("maxScale", -1)
            scale = (
                self._convert_p2p_scale_to_standard(max_scale_raw)
                if max_scale_raw != -1
                else None
            )

            # 深度解析
            depth_raw = hypocenter.get("depth")
            depth = self._safe_float_convert(depth_raw)

            # 时间解析
            time_raw = earthquake_info.get("time", "")
            shock_time = self._parse_datetime(time_raw)

            earthquake = EarthquakeData(
                id=data.get("id", ""),  # P2P使用"id"字段
                event_id=data.get("id", ""),  # 同样用作event_id
                source=DataSource.P2P_EARTHQUAKE,
                disaster_type=DisasterType.EARTHQUAKE,
                shock_time=shock_time,
                latitude=lat,
                longitude=lon,
                depth=depth,
                magnitude=magnitude,
                place_name=place_name or "未知地点",
                scale=scale,
                max_scale=max_scale_raw,
                domestic_tsunami=earthquake_info.get("domesticTsunami"),
                foreign_tsunami=earthquake_info.get("foreignTsunami"),
                raw_data=data,
            )

            logger.info(
                f"[灾害预警] 地震数据解析成功: {earthquake.place_name} (M {earthquake.magnitude}), 时间: {earthquake.shock_time}"
            )

            return DisasterEvent(
                id=earthquake.id,
                data=earthquake,
                source=earthquake.source,
                disaster_type=earthquake.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析地震情報失败: {e}")
            return None

    def _convert_p2p_scale_to_standard(self, p2p_scale: int) -> float | None:
        """将P2P震度值转换为标准震度 - 补充完整枚举值"""
        scale_mapping = {
            -1: None,  # 震度情報不存在
            0: 0.0,  # 震度0
            10: 1.0,  # 震度1
            20: 2.0,  # 震度2
            30: 3.0,  # 震度3
            40: 4.0,  # 震度4
            45: 4.5,  # 震度5弱
            46: 4.6,  # 震度5弱以上と推定されるが震度情報を入手していない（推测震度为5弱以上，但尚未获取震级信息）
            50: 5.0,  # 震度5強
            55: 5.5,  # 震度6弱
            60: 6.0,  # 震度6強
            70: 7.0,  # 震度7
        }

        if p2p_scale not in scale_mapping:
            logger.warning(f"[灾害预警] {self.source_id} 未知的P2P震度值: {p2p_scale}")
            return None

        return scale_mapping.get(p2p_scale)


class JMAEarthquakeWolfxHandler(BaseDataHandler):
    """日本气象厅地震情报处理器 - Wolfx"""

    def __init__(self, message_logger=None):
        super().__init__("jma_wolfx_info", message_logger)

    def _parse_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析Wolfx日本气象厅地震列表"""
        try:
            # 检查消息类型
            if data.get("type") != "jma_eqlist":
                logger.debug(f"[灾害预警] {self.source_id} 非JMA地震列表数据，跳过")
                return None

            # 只处理最新的地震
            eq_info = None
            for key, value in data.items():
                if key.startswith("No") and isinstance(value, dict):
                    eq_info = value
                    break

            if not eq_info:
                return None

            # 修复深度字段格式 - 处理"20km"字符串格式
            depth_raw = eq_info.get("depth")
            depth = None
            if depth_raw:
                if isinstance(depth_raw, str) and depth_raw.endswith("km"):
                    try:
                        depth = float(depth_raw[:-2])  # 去掉"km"后缀
                    except (ValueError, TypeError):
                        depth = None
                else:
                    depth = self._safe_float_convert(depth_raw)

            # 修复震级字段格式
            magnitude_raw = eq_info.get("magnitude")
            magnitude = self._safe_float_convert(magnitude_raw)

            earthquake = EarthquakeData(
                id=eq_info.get("md5", ""),
                event_id=eq_info.get("md5", ""),
                source=DataSource.WOLFX_JMA_EEW,
                disaster_type=DisasterType.EARTHQUAKE,
                shock_time=self._parse_datetime(eq_info.get("time", "")),
                latitude=self._safe_float_convert(eq_info.get("latitude")),
                longitude=self._safe_float_convert(eq_info.get("longitude")),
                depth=depth,
                magnitude=magnitude,
                scale=self._parse_jma_scale(eq_info.get("shindo", "")),
                place_name=eq_info.get("location", ""),
                raw_data=data,
            )

            logger.info(
                f"[灾害预警] 地震数据解析成功: {earthquake.place_name} (M {earthquake.magnitude}), 时间: {earthquake.shock_time}"
            )

            return DisasterEvent(
                id=earthquake.id,
                data=earthquake,
                source=earthquake.source,
                disaster_type=earthquake.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析数据失败: {e}")
            return None

    def _parse_jma_scale(self, scale_str: str) -> float | None:
        """解析日本震度"""
        if not scale_str:
            return None

        import re

        match = re.search(r"(\d+)(弱|強)?", scale_str)
        if match:
            base = int(match.group(1))
            suffix = match.group(2)

            if suffix == "弱":
                return base - 0.5
            elif suffix == "強":
                return base + 0.5
            else:
                return float(base)

        return None


class USGSEarthquakeHandler(BaseDataHandler):
    """美国地质调查局地震情报处理器"""

    def __init__(self, message_logger=None):
        super().__init__("usgs_fanstudio", message_logger)

    def _parse_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析USGS地震数据"""
        try:
            # 获取实际数据 - 兼容多种格式
            msg_data = data.get("Data", {}) or data.get("data", {}) or data
            if not msg_data:
                logger.debug(f"[灾害预警] {self.source_id} 消息中没有有效数据")
                return None

            # 记录数据获取情况用于调试
            if "Data" in data:
                logger.debug(f"[灾害预警] {self.source_id} 使用Data字段获取数据")
            elif "data" in data:
                logger.debug(f"[灾害预警] {self.source_id} 使用data字段获取数据")
            else:
                logger.debug(f"[灾害预警] {self.source_id} 使用整个消息作为数据")

            # 心跳包检测 - 在详细处理前进行快速过滤
            if self._is_heartbeat_message(msg_data):
                return None

            # 检查关键字段 - 兼容大小写（仅记录警告，不阻止处理）
            required_fields = ["id", "magnitude", "latitude", "longitude", "shockTime"]
            missing_fields = []
            for field in required_fields:
                # 检查小写和大写版本
                if field not in msg_data and field.capitalize() not in msg_data:
                    missing_fields.append(field)
                elif field in msg_data and msg_data[field] is None:
                    missing_fields.append(field)
                elif (
                    field.capitalize() in msg_data
                    and msg_data[field.capitalize()] is None
                ):
                    missing_fields.append(field)

            if missing_fields:
                logger.debug(
                    f"[灾害预警] {self.source_id} 数据缺少部分字段: {missing_fields}，继续处理..."
                )

            # 优化USGS数据精度 - 四舍五入到1位小数
            def get_field(data, field_name):
                """获取字段值，兼容大小写"""
                return data.get(field_name) or data.get(field_name.capitalize())

            magnitude_raw = get_field(msg_data, "magnitude")
            if magnitude_raw is not None:
                try:
                    magnitude = round(float(magnitude_raw), 1)
                except (ValueError, TypeError):
                    magnitude = None
            else:
                magnitude = None

            depth_raw = get_field(msg_data, "depth")
            if depth_raw is not None:
                try:
                    depth = round(float(depth_raw), 1)
                except (ValueError, TypeError):
                    depth = None
            else:
                depth = None

            # 兼容大小写字段名
            def get_field(data, field_name):
                """获取字段值，兼容大小写"""
                return data.get(field_name) or data.get(field_name.capitalize())

            # 关键数据验证 - 防止空内容推送
            usgs_id = get_field(msg_data, "id") or ""
            usgs_latitude = float(get_field(msg_data, "latitude") or 0)
            usgs_longitude = float(get_field(msg_data, "longitude") or 0)
            usgs_place_name = get_field(msg_data, "placeName") or ""

            # 验证关键字段 - 如果缺少关键信息，不创建地震对象
            if not usgs_id:
                # 只有在非心跳包情况下才记录警告，且避免重复警告
                if not self._is_heartbeat_message(msg_data):
                    warning_msg = f"[灾害预警] {self.source_id} 缺少地震ID，跳过处理"
                    if self._should_log_warning("missing_usgs_id", warning_msg):
                        logger.warning(warning_msg)
                return None

            if usgs_latitude == 0 and usgs_longitude == 0:
                # 心跳包检测已经处理了这种情况，这里不再重复记录
                return None

            if not usgs_place_name and not magnitude:
                # 只有在非心跳包情况下才记录警告，且避免重复警告
                if not self._is_heartbeat_message(msg_data):
                    warning_msg = (
                        f"[灾害预警] {self.source_id} 缺少地点名称和震级信息，跳过处理"
                    )
                    if self._should_log_warning(
                        "missing_usgs_place_magnitude", warning_msg
                    ):
                        logger.warning(warning_msg)
                return None

            earthquake = EarthquakeData(
                id=usgs_id,
                event_id=usgs_id,
                source=DataSource.FAN_STUDIO_USGS,
                disaster_type=DisasterType.EARTHQUAKE,
                shock_time=self._parse_datetime(get_field(msg_data, "shockTime")),
                update_time=self._parse_datetime(get_field(msg_data, "updateTime")),
                latitude=usgs_latitude,
                longitude=usgs_longitude,
                depth=depth,
                magnitude=magnitude,
                place_name=usgs_place_name,
                info_type=get_field(msg_data, "infoTypeName") or "",
                raw_data=msg_data,
            )

            logger.info(
                f"[灾害预警] 地震数据解析成功: {earthquake.place_name} (M {earthquake.magnitude}), 时间: {earthquake.shock_time}"
            )

            return DisasterEvent(
                id=earthquake.id,
                data=earthquake,
                source=earthquake.source,
                disaster_type=earthquake.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析数据失败: {e}")
            return None


class ChinaWeatherHandler(BaseDataHandler):
    """中国气象局气象预警处理器"""

    def __init__(self, message_logger=None):
        super().__init__("china_weather_fanstudio", message_logger)

    def _parse_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析中国气象局气象预警数据"""
        try:
            # 获取实际数据 - 兼容多种格式
            msg_data = data.get("Data", {}) or data.get("data", {}) or data
            if not msg_data:
                logger.debug(f"[灾害预警] {self.source_id} 消息中没有有效数据")
                return None

            # 记录数据获取情况用于调试
            if "Data" in data:
                logger.debug(f"[灾害预警] {self.source_id} 使用Data字段获取数据")
            elif "data" in data:
                logger.debug(f"[灾害预警] {self.source_id} 使用data字段获取数据")
            else:
                logger.debug(f"[灾害预警] {self.source_id} 使用整个消息作为数据")

            # 心跳包检测 - 在详细处理前进行快速过滤
            if self._is_heartbeat_message(msg_data):
                return None

            # 检查关键字段
            required_fields = ["id", "headline", "effective", "description"]
            missing_fields = [
                field
                for field in required_fields
                if field not in msg_data or msg_data[field] is None
            ]
            if missing_fields:
                logger.debug(
                    f"[灾害预警] {self.source_id} 气象预警数据缺少关键字段: {missing_fields}"
                )

            # 提取真实的生效时间
            effective_time = self._parse_datetime(msg_data.get("effective", ""))

            # 尝试从ID中提取生效时间
            issue_time = None
            id_str = msg_data.get("id", "")
            if "_" in id_str:
                time_part = id_str.split("_")[-1]
                if len(time_part) >= 12:
                    try:
                        year = int(time_part[0:4])
                        month = int(time_part[4:6])
                        day = int(time_part[6:8])
                        hour = int(time_part[8:10])
                        minute = int(time_part[10:12])
                        second = int(time_part[12:14]) if len(time_part) >= 14 else 0
                        issue_time = datetime(year, month, day, hour, minute, second)
                    except (ValueError, IndexError):
                        issue_time = effective_time
                else:
                    issue_time = effective_time
            else:
                issue_time = effective_time

            # 验证关键字段，防止空信息推送
            headline = msg_data.get("headline", "")
            title = msg_data.get("title", "")
            description = msg_data.get("description", "")

            if not headline and not title and not description:
                # 只有在非心跳包情况下才记录
                if not self._is_heartbeat_message(msg_data):
                    warning_msg = f"[灾害预警] {self.source_id} 气象预警缺少标题、名称和描述信息，跳过处理"
                    if self._should_log_warning("missing_weather_fields", warning_msg):
                        logger.debug(warning_msg)
                return None

            weather = WeatherAlarmData(
                id=msg_data.get("id", ""),
                source=DataSource.FAN_STUDIO_WEATHER,
                headline=headline,
                title=title,
                description=description,
                type=msg_data.get("type", ""),
                effective_time=effective_time,
                issue_time=issue_time,
                longitude=msg_data.get("longitude"),
                latitude=msg_data.get("latitude"),
                raw_data=msg_data,
            )

            logger.info(
                f"[灾害预警] 气象预警解析成功: {weather.headline}, 生效时间: {weather.issue_time}"
            )

            return DisasterEvent(
                id=weather.id,
                data=weather,
                source=weather.source,
                disaster_type=weather.disaster_type,
            )
        except Exception as e:
            logger.error(
                f"[灾害预警] {self.source_id} 解析气象预警数据失败: {e}, 数据内容: {data}"
            )
            return None


class ChinaTsunamiHandler(BaseDataHandler):
    """中国海啸预警处理器"""

    def __init__(self, message_logger=None):
        super().__init__("china_tsunami_fanstudio", message_logger)

    def _parse_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析中国海啸预警数据"""
        try:
            # 获取实际数据 - 兼容多种格式
            msg_data = data.get("Data", {}) or data.get("data", {}) or data
            if not msg_data:
                logger.debug(f"[灾害预警] {self.source_id} 消息中没有有效数据")
                return None

            # 记录数据获取情况用于调试
            if "Data" in data:
                logger.debug(f"[灾害预警] {self.source_id} 使用Data字段获取数据")
            elif "data" in data:
                logger.debug(f"[灾害预警] {self.source_id} 使用data字段获取数据")
            else:
                logger.debug(f"[灾害预警] {self.source_id} 使用整个消息作为数据")

            # 心跳包检测 - 在详细处理前进行快速过滤
            if self._is_heartbeat_message(msg_data):
                return None

            # 海啸数据可能包含多个事件，只处理第一个
            events = []
            if isinstance(msg_data, dict):
                events = [msg_data]
            elif isinstance(msg_data, list):
                events = msg_data

            if not events:
                return None

            tsunami_data = events[0]

            # 提取真实的时间信息 - 优先使用alarmDate作为发布时间
            time_info = tsunami_data.get("timeInfo", {})
            issue_time_str = (
                time_info.get("alarmDate")
                or time_info.get("issueTime")
                or time_info.get("publishTime")
                or time_info.get("updateDate")
                or ""
            )

            if issue_time_str:
                issue_time = self._parse_datetime(issue_time_str)
            else:
                # 后备方案：使用当前时间
                issue_time = datetime.now()

            # 验证关键字段，防止空信息推送
            title = tsunami_data.get("warningInfo", {}).get("title", "")
            level = tsunami_data.get("warningInfo", {}).get("level", "")

            if not title and not level:
                # 只有在非心跳包情况下才记录
                if not self._is_heartbeat_message(msg_data):
                    warning_msg = f"[灾害预警] {self.source_id} 海啸预警缺少标题和级别信息，跳过处理"
                    if self._should_log_warning("missing_tsunami_fields", warning_msg):
                        logger.debug(warning_msg)
                return None

            tsunami = TsunamiData(
                id=tsunami_data.get("id", ""),
                code=tsunami_data.get("code", ""),
                source=DataSource.FAN_STUDIO_TSUNAMI,
                title=title,
                level=level,
                subtitle=tsunami_data.get("warningInfo", {}).get("subtitle"),
                org_unit=tsunami_data.get("warningInfo", {}).get("orgUnit", ""),
                issue_time=issue_time,
                forecasts=tsunami_data.get("forecasts", []),
                monitoring_stations=tsunami_data.get("waterLevelMonitoring", []),
                raw_data=tsunami_data,
            )

            logger.info(
                f"[灾害预警] 海啸预警解析成功: {tsunami.title}, 级别: {tsunami.level}, 发布时间: {tsunami.issue_time}"
            )

            return DisasterEvent(
                id=tsunami.id,
                data=tsunami,
                source=tsunami.source,
                disaster_type=tsunami.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析海啸预警数据失败: {e}")
            return None


class JMATsunamiP2PHandler(BaseDataHandler):
    """日本气象厅海啸预报处理器 - P2P专用"""

    def __init__(self, message_logger=None):
        super().__init__("jma_tsunami_p2p", message_logger)

    def parse_message(self, message: str) -> DisasterEvent | None:
        """解析P2P海啸预报消息"""
        try:
            data = json.loads(message)

            # 根据code判断消息类型
            code = data.get("code")

            if code == 552:  # 津波予報
                logger.debug(f"[灾害预警] {self.source_id} 收到津波予報(code:552)")
                return self._parse_tsunami_data(data)
            else:
                logger.debug(
                    f"[灾害预警] {self.source_id} 非海啸预报数据，code: {code}"
                )
                return None

        except json.JSONDecodeError as e:
            logger.error(f"[灾害预警] {self.source_id} JSON解析失败: {e}")
            return None
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 消息处理失败: {e}")
            return None

    def _parse_tsunami_data(self, data: dict[str, Any]) -> DisasterEvent | None:
        """解析P2P津波予報数据 - 基于日本气象厅实际字段"""
        try:
            # 获取基础数据 - 使用P2P标准字段名
            issue_info = data.get("issue", {})
            areas = data.get("areas", [])
            cancelled = data.get("cancelled", False)

            # 检查是否为取消报文
            if cancelled:
                logger.info(f"[灾害预警] {self.source_id} 收到津波予報解除信息")
                # 创建解除事件
                tsunami = TsunamiData(
                    id=data.get("id", ""),
                    code="552",
                    source=DataSource.P2P_TSUNAMI,
                    title="津波予報解除",
                    level="解除",
                    issue_time=self._parse_datetime(data.get("time", "")),
                    forecasts=[],  # 解除时报文区域为空
                    raw_data=data,
                )
            else:
                # 处理正常津波予報
                if not areas:
                    logger.warning(f"[灾害预警] {self.source_id} 津波予報缺少区域信息")
                    return None

                # 兼容性处理：检查必填字段
                required_issue_fields = ["source", "time", "type"]
                missing_fields = []
                for field in required_issue_fields:
                    if field not in issue_info:
                        missing_fields.append(field)

                if missing_fields:
                    logger.warning(
                        f"[灾害预警] {self.source_id} 缺少issue必填字段: {missing_fields}，继续处理..."
                    )

                # 构建预报区域列表 - 基于P2P实际字段结构
                forecasts = []
                for area in areas:
                    forecast = {
                        "name": area.get("name", ""),
                        "grade": area.get("grade", ""),
                        "immediate": area.get("immediate", False),
                    }

                    # 处理firstHeight信息
                    first_height = area.get("firstHeight", {})
                    if first_height:
                        if "arrivalTime" in first_height:
                            forecast["estimatedArrivalTime"] = first_height.get(
                                "arrivalTime"
                            )
                        if "condition" in first_height:
                            forecast["condition"] = first_height.get("condition")

                    # 处理maxHeight信息
                    max_height = area.get("maxHeight", {})
                    if max_height:
                        if "description" in max_height:
                            forecast["maxWaveHeight"] = max_height.get("description")
                        if "value" in max_height:
                            forecast["maxHeightValue"] = max_height.get("value")

                    if forecast["name"]:  # 只添加有名称的区域
                        forecasts.append(forecast)

                if not forecasts:
                    logger.warning(f"[灾害预警] {self.source_id} 没有有效的预报区域")
                    return None

                # 确定警报级别 - 基于最高级别
                alert_levels = {
                    "MajorWarning": "大津波警報",
                    "Warning": "津波警報",
                    "Watch": "津波注意報",
                    "Unknown": "不明",
                }
                max_level = "Unknown"
                for area in areas:
                    grade = area.get("grade", "")
                    if grade == "MajorWarning":
                        max_level = "MajorWarning"
                        break
                    elif grade == "Warning" and max_level != "MajorWarning":
                        max_level = "Warning"
                    elif grade == "Watch" and max_level not in [
                        "MajorWarning",
                        "Warning",
                    ]:
                        max_level = "Watch"

                # 构建标题
                title = alert_levels.get(max_level, "津波予報")

                tsunami = TsunamiData(
                    id=data.get("id", ""),
                    code="552",
                    source=DataSource.P2P_TSUNAMI,
                    title=title,
                    level=max_level,
                    org_unit=issue_info.get("source", "日本气象厅"),
                    issue_time=self._parse_datetime(issue_info.get("time", ""))
                    or self._parse_datetime(data.get("time", "")),
                    forecasts=forecasts,
                    raw_data=data,
                )

            logger.info(
                f"[灾害预警] P2P津波予報解析成功: {tsunami.title}, 级别: {tsunami.level}, "
                f"区域数: {len(tsunami.forecasts)}, 发布时间: {tsunami.issue_time}"
            )

            return DisasterEvent(
                id=tsunami.id,
                data=tsunami,
                source=tsunami.source,
                disaster_type=tsunami.disaster_type,
            )
        except Exception as e:
            logger.error(f"[灾害预警] {self.source_id} 解析P2P津波予報数据失败: {e}")
            logger.error(f"[灾害预警] 异常堆栈: {traceback.format_exc()}")
            return None


# 辅助方法
def _safe_float_convert(value) -> float | None:
    """安全地将值转换为浮点数"""
    if value is None:
        return None
    try:
        # 处理字符串情况
        if isinstance(value, str):
            value = value.strip()
            if not value or value == "":
                return None
        return float(value)
    except (ValueError, TypeError):
        return None


# 处理器映射
DATA_HANDLERS = {
    # EEW预警处理器
    "cea_fanstudio": CEAEEWHandler,
    "cea_wolfx": CEAEEWWolfxHandler,
    "cwa_fanstudio": CWAEEWHandler,
    "cwa_wolfx": CWAEEWWolfxHandler,
    "jma_p2p": JMAEEWP2PHandler,
    "jma_wolfx": JMAEEWWolfxHandler,
    "global_quake": GlobalQuakeHandler,
    # 地震情报处理器
    "cenc_fanstudio": CENCEarthquakeHandler,
    "cenc_wolfx": CENCEarthquakeWolfxHandler,
    "jma_p2p_info": JMAEarthquakeP2PHandler,
    "jma_wolfx_info": JMAEarthquakeWolfxHandler,
    "usgs_fanstudio": USGSEarthquakeHandler,
    # 气象和海啸预警处理器
    "china_weather_fanstudio": ChinaWeatherHandler,
    "china_tsunami_fanstudio": ChinaTsunamiHandler,
    "jma_tsunami_p2p": JMATsunamiP2PHandler,
}


def get_data_handler(source_id: str, message_logger=None):
    """获取数据处理器"""
    handler_class = DATA_HANDLERS.get(source_id)
    if handler_class:
        return handler_class(message_logger)
    return None
