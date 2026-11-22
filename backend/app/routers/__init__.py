"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import recompute, resource

# recompute 含多段专用路径，必须在通用 /{module}/{entry_id} 之前注册。
ROUTERS = [recompute, resource]
