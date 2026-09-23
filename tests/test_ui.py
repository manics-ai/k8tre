import os
from pathlib import Path
import subprocess
import time
import pytest
from PIL import Image, ImageChops
from playwright.sync_api import Page, expect

K8TRE_DOMAIN = os.getenv("K8TRE_DOMAIN", "dev.k8tre.internal")

HERE = Path(__file__).absolute().parent


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args, playwright):
    return {
        "ignore_https_errors": True,
        "record_video_dir": "screenshots",
        "viewport": {"width": 1280, "height": 720},
    }


def compare_screenshot(test_image, threshold=6, throw=True):
    # Compare images by calculating the mean absolute difference
    # Images must be the same size
    # threshold: Average difference per pixel, this depends on the image type
    # e.g. for 24 bit images (8 bit RGB pixels) threshold=1 means a maximum
    # difference of 1 bit per pixel per channel
    reference = Image.open(HERE / "reference" / "desktop.png")
    test = Image.open(test_image)

    # Absolute difference
    # Convert to RGB, alpha channel breaks ImageChops
    diff = ImageChops.difference(reference.convert("RGB"), test.convert("RGB"))
    width, height = diff.size
    try:
        data = diff.get_flattened_data()
    except AttributeError:
        data = diff.getdata()

    m = sum(sum(px) for px in data) / width / height
    if throw:
        assert m < threshold, f"Mean pixel difference {m} exceeds threshold {threshold}"
    return m < threshold


def login_to_portal(page: Page) -> None:
    """Login to Portal via Keycloak SSO, completing profile if prompted."""
    page.goto(f"https://portal.{K8TRE_DOMAIN}/login")
    page.wait_for_load_state("networkidle")

    # Keycloak login form
    if page.get_by_role("textbox", name="Username or email").is_visible():
        page.get_by_role("textbox", name="Username or email").fill("trevolution")
        page.get_by_role("textbox", name="Password").fill("k8tre")
        page.get_by_role("button", name="Sign In").click()
        page.wait_for_load_state("networkidle")

    # If Keycloak prompts to complete user profile on first login (Update Account Information)
    if page.locator("input#firstName").is_visible():
        page.locator("input#firstName").fill("TRE")
        page.locator("input#lastName").fill("User")
        page.locator("input[type='submit']").click()
        page.wait_for_load_state("networkidle")

    # Wait for redirect back to portal
    page.wait_for_url(f"https://portal.{K8TRE_DOMAIN}/**")
    page.wait_for_load_state("networkidle")


def navigate_to_project_apps(page: Page, project: str) -> None:
    """Navigate to a project apps page by checking and clicking links on the page."""
    apps_url_pattern = f"/projects/{project}/apps"
    apps_link = page.locator(f"a[href='{apps_url_pattern}']")

    if not apps_link.is_visible():
        projects_link = page.locator("a[href='/projects']").first
        if projects_link.is_visible():
            projects_link.click()
            page.wait_for_load_state("networkidle")

    expect(apps_link).to_be_visible()
    apps_link.click()
    page.wait_for_load_state("networkidle")


def get_vdi_window_titles(project: str = "diabetes", user: str = "trevolution") -> str:
    """Retrieve list of active X11 window titles from inside the VDI container."""
    cmd = [
        "kubectl", "exec", "-n", f"project-{project}", f"vdi-{user}-{project}", "--",
        "sh", "-c",
        f"export XAUTHORITY=/home/vdx-{user}-{project}/.Xauthority; "
        "for id in $(xprop -display :10 -root _NET_CLIENT_LIST 2>/dev/null | cut -d# -f2 | tr ',' ' '); do "
        "  xprop -display :10 -id $id _NET_WM_NAME 2>/dev/null; "
        "done"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.stdout


@pytest.mark.ui
def test_portal_guacamole_vdi(page: Page) -> None:
    """Test Portal login and Guacamole VDI launch with desktop verification."""
    # 1. Login to Portal via Keycloak SSO
    login_to_portal(page)

    # 2. Select Project Apps
    navigate_to_project_apps(page, "asthma")

    # 3. Launch Guacamole VDI
    page.locator("a[href='/launch/asthma/guacamole']").click()
    page.wait_for_load_state("networkidle")

    # Wait for automatic redirection to Guacamole
    page.wait_for_url("**/guacamole/**", timeout=60000)

    # Wait for desktop to load, take a screenshot to compare
    screenshot = Path("screenshots") / "desktop.png"
    screenshot.parent.mkdir(exist_ok=True)

    matched = False
    for i in range(15):
        page.wait_for_timeout(5000)
        page.screenshot(path=screenshot)
        if compare_screenshot(screenshot, threshold=6, throw=False):
            matched = True
            break
        print(f"Screenshot attempt {i+1} doesn't match yet...")

    assert matched, "Guacamole desktop screenshot did not match reference"

    # 4. Cleanup: Shutdown VDI instance via portal
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(f"https://portal.{K8TRE_DOMAIN}/vdi")
    page.wait_for_load_state("networkidle")
    shutdown_btn = page.locator("form[action='/shutdown-vdi'] button[type='submit']")
    if shutdown_btn.count() > 0:
        shutdown_btn.first.click()
        page.wait_for_load_state("networkidle")


@pytest.mark.ui
def test_portal_jupyterhub_launch(page: Page) -> None:
    """Test Portal login and JupyterHub workspace launch."""
    # 1. Login to Portal via Keycloak SSO
    login_to_portal(page)

    # 2. Navigate to Asthma project apps
    navigate_to_project_apps(page, "asthma")

    # 3. Launch JupyterHub
    page.locator("a[href='/launch/asthma/jupyterhub']").click()
    page.wait_for_url("**/hub/**", timeout=30000)

    # 4. Verify profile selection form displays dynamic profile from backend
    expect(page.get_by_text("Asthma Workspace 1")).to_be_visible()


@pytest.mark.ui
def test_vdi_jupyterhub_in_browser(page: Page) -> None:
    """Verify that JupyterHub opens in Firefox inside the Guacamole VDI session (stricter model)."""
    # 1. Login to Portal via Keycloak SSO
    login_to_portal(page)

    # 2. Check that the project apps URL is present on the page and click it (L163)
    navigate_to_project_apps(page, "diabetes")

    # 3. Launch Guacamole VDI and wait for remote desktop canvas
    page.locator("a[href='/launch/diabetes/guacamole']").click()
    page.wait_for_url("**/guacamole/**", timeout=60000)
    page.wait_for_selector("div.display", timeout=30000)
    page.wait_for_timeout(3000)

    # 4. Method A: Focus remote desktop and open Application Finder via Alt+F2
    page.mouse.click(640, 384)
    page.wait_for_timeout(500)
    page.keyboard.press("Alt+F2")

    for _ in range(10):
        time.sleep(1)
        if "Application Finder" in get_vdi_window_titles("diabetes"):
            break

    # Click inside Application Finder input and launch Firefox with JupyterHub URL
    page.mouse.click(550, 385)
    page.wait_for_timeout(500)
    launch_url = f"https://portal.{K8TRE_DOMAIN}/launch/diabetes/jupyterhub"
    page.keyboard.type(f"firefox {launch_url}", delay=20)
    page.wait_for_timeout(500)
    page.keyboard.press("Enter")

    # 5. Wait for Firefox and handle Keycloak login redirect if prompted
    for _ in range(15):
        time.sleep(2)
        wins = get_vdi_window_titles("diabetes")
        if "Firefox" in wins:
            break

    jupyter_found = False
    for i in range(25):
        time.sleep(2)
        wins = get_vdi_window_titles("diabetes")
        if "Jupyter" in wins:
            jupyter_found = True
            break
        if "Sign in" in wins:
            # Handle Keycloak login inside VDI browser
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            page.mouse.click(640, 450)
            page.wait_for_timeout(500)
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            page.keyboard.type("trevolution", delay=30)
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            page.keyboard.type("k8tre", delay=30)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1000)
            page.mouse.click(640, 630)

    # 6. Capture screenshot of Guacamole canvas showing JupyterHub inside VDI
    screenshot = Path("screenshots") / "vdi_jupyterhub.png"
    page.screenshot(path=screenshot)

    assert jupyter_found, f"Jupyter window not found in VDI X11 display. Windows: {get_vdi_window_titles('diabetes')}"
    assert screenshot.exists(), "Screenshot was not saved"

    # 7. Cleanup: Shutdown VDI instance via portal
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(f"https://portal.{K8TRE_DOMAIN}/vdi")
    page.wait_for_load_state("networkidle")
    shutdown_btn = page.locator("form[action='/shutdown-vdi'] button[type='submit']")
    if shutdown_btn.count() > 0:
        shutdown_btn.first.click()
        page.wait_for_load_state("networkidle")

