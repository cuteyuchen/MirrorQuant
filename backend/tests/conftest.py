import os
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# Add app directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

# Configure test environment variables BEFORE importing app config
os.environ["MQ_ENV"] = "test"
os.environ["MQ_ENABLE_BACKGROUND_ENGINE"] = "false"
os.environ["MQ_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["MQ_MASTER_KEY"] = "TestMasterKeyForAES256GCMEncryptionMustBeStrong32!"
os.environ["MQ_JWT_SECRET"] = "TestJWTSecretKeyForTokenSigningAndValidation32!"

from app.core.database import get_db, init_db, reset_db_engine
from app.main import app


@pytest.fixture(autouse=True)
async def setup_test_db():
    await reset_db_engine()
    await init_db()
    yield
    await reset_db_engine()


@pytest.fixture
async def async_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
