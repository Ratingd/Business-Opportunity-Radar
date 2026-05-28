import os
import subprocess
from playwright._impl._driver import compute_driver_executable

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(os.getcwd(), "test_browsers")
driver_executable, driver_cli = compute_driver_executable()

env = os.environ.copy()
result = subprocess.run([driver_executable, driver_cli, "install", "chromium"], env=env, capture_output=True, text=True)
print("RC:", result.returncode)
print("STDOUT:", result.stdout)
print("STDERR:", result.stderr)
