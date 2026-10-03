def pytest_configure(config):
    # evals/ has no pytest ini of its own (backend's is not found from here), so the marker
    # the backend suite registers in pyproject.toml has to be declared again.
    config.addinivalue_line(
        "markers", "sandbox: needs a working Docker daemon; runs real containers"
    )
