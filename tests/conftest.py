"""pytest 共享配置：把项目根目录加入 sys.path。

使测试无论从项目根（`python3 -m pytest`）还是从 tests/ 目录运行，
都能直接 `import core.downloader` / `import core.metadata`。
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# webapp/ 顶层使用 `from models import ...` 等绝对导入，
# `import task_manager` 需要额外注入 webapp/ 目录
WEBAPP_DIR = PROJECT_ROOT / "webapp"
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))


@pytest.fixture
def webapp_app():
    """提供自建 Flask app + 内存 SQLite 的 function 级 fixture。

    不 import webapp/app.py（无 create_app 工厂，模块级副作用多）。
    db 是 models 模块级单例：每个测试用全新 app 重复 init_app 是
    允许的（同一 app 不重复 init），内存库随 app/engine 每测试重建。
    yield (app, ) 后 pop context 清理。
    """
    from flask import Flask
    from models import db

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
    ctx = app.app_context()
    ctx.push()
    yield (app,)
    ctx.pop()
