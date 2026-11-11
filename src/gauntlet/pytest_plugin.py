"""Registers the gauntlet(class_id, site) marker in every pytest run that has the package installed."""


def pytest_configure(config):
    config.addinivalue_line("markers", "gauntlet(class_id, site): binds this test to a gauntlet class at a site")
