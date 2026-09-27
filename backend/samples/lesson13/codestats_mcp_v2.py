"""
一个最小化 mcp 示例
用于统计项目中的 代码行数
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from mcp.server import MCPServer


logger = logging.getLogger(__name__)


def _resolve_project(root: Path, project_name: str) -> Path:
    project_dir = (root / project_name).resolve()
    try:
        project_dir.relative_to(root)
    except ValueError as exc:
        logger.warning(f"项目必须位于 CODE_STATS_ROOT 目录内: {project_name}")  
        raise ValueError("项目必须位于 CODE_STATS_ROOT 目录内") from exc
    if not project_dir.is_dir():
        logger.warning(f"项目目录不存在: {project_name}")
        raise ValueError(f"项目目录不存在: {project_name}")
    return project_dir


def _count_python_lines(project_dir: Path) -> int:
    total_lines = 0
    for file_path in project_dir.rglob("*.py"):
        if ".venv" in file_path.relative_to(project_dir).parts:
            continue
        try:
            with file_path.open("r", encoding="utf-8") as source:
                total_lines += sum(1 for _ in source)
        except OSError as exc:
            logger.warning("无法读取文件 %s: %s", file_path, exc)
    return total_lines

async def get_current_weather(city_name: str) -> dict[str, object]:
    city_name = city_name.strip()
    if not city_name or len(city_name) > 100:
        raise ValueError("请提供长度不超过 100 字符的城市名")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            geo_response = await client.get("https://geocoding-api.open-meteo.com/v1/search",
                                            params={"name": city_name, "language": "zh"})
            geo_response.raise_for_status()
            locations = geo_response.json().get("results") or []
            if not locations:
                raise ValueError(f"未找到城市：{city_name}")

            location = locations[0]
            weather_response = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": location["latitude"],
                        "longitude": location["longitude"],
                        "current": ("temperature_2m,relative_humidity_2m,"
                                    "weather_code"), "timezone": "auto",
                        },
            )
            weather_response.raise_for_status()
            data = weather_response.json()

    except httpx.HTTPError as exc:
        logger.exception("天气服务请求失败")
        raise RuntimeError("天气服务暂时不可用，请稍后重试") from exc

    current = data.get("current")
    if not isinstance(current, dict):
        raise RuntimeError("天气服务未返回当前天气数据")

    return {
        "location": location["name"],
        "country": location.get("country"),
        "timezone": data.get("timezone"),
        "observed_at": current.get("time"),
        "temperature_c": current.get("temperature_2m"),
        "relative_humidity_percent": current.get("relative_humidity_2m"),
        "weather_code": current.get("weather_code"),
        "source": "Open-Meteo",
    }

def build_server(base_dir: str | Path) -> MCPServer:
    root = Path(base_dir).expanduser().resolve()
    logger.info("CODE_STATS_ROOT: %s", root)
    server = MCPServer("CodeStats")

    @server.tool()
    def count_python_lines(project_name: str) -> int:
        """统计指定项目中 Python 代码的总行数。"""
        return _count_python_lines(_resolve_project(root, project_name))

    @server.resource("info://{project_name}")
    def get_project_info(project_name: str) -> str:
        """读取项目的 Python 代码行数摘要。"""
        # project_dir = _resolve_project(root, project_name)
        # lines = _count_python_lines(project_dir)
        lines = "测试数据"
        return f"项目 {project_name} 包含 {lines} 行 Python 代码"

    # 新增天气测试Tool
    @server.tool()
    async def get_weather(city_name: str) -> dict[str, object]:
        """查询指定城市的当前天气，返回地点、数据时间、气温、湿度和天气代码。"""
        return await get_current_weather(city_name)

    return server


mcp = build_server(os.environ.get("CODE_STATS_ROOT", str(Path.cwd())))


# uv run mcp dev samples/lesson13/codestats_mcp_v2.py
if __name__ == "__main__":
    mcp.run("stdio")
