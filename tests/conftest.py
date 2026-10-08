# tests/conftest.py
import pytest


@pytest.fixture(scope="session")
def anyio_backend():
    # Las pruebas asíncronas usan asyncio (no hace falta instalar trio).
    return "asyncio"
