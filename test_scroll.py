import asyncio
import os
from src.utils.nodriver_browser import start_browser

async def main():
    print("Starting browser...")
    # Start the browser headful so you can see it
    browser = await start_browser(headless=False)
    
    html_path = f"file://{os.path.abspath('test_scroll.html')}"
    page = await browser.get(html_path)
    
    print("Page loaded. Waiting 2 seconds...")
    await page.sleep(2)
    
    print("\n1. Breaking the glass layer...")
    # Break the glass layer with a real click
    try:
        import nodriver.cdp.input_ as cdp_input
        await page.send(cdp_input.dispatch_mouse_event(
            type_="mousePressed", x=100, y=100, button=cdp_input.MouseButton.LEFT, click_count=1
        ))
        await page.sleep(0.05)
        await page.send(cdp_input.dispatch_mouse_event(
            type_="mouseReleased", x=100, y=100, button=cdp_input.MouseButton.LEFT, click_count=1
        ))
    except Exception as e:
        print(f"Click failed: {e}")
        
    await page.sleep(1)
    
    # Close any new popup/ad tabs that opened
    print(f"Tabs open before closing popups: {len(browser.tabs) if hasattr(browser, 'tabs') else 'unknown'}")
    if hasattr(browser, 'tabs'):
        for tab in list(browser.tabs):
            if tab != page and tab != browser.main_tab:
                try:
                    await tab.close()
                    print("-> Closed a popup tab!")
                except Exception as e:
                    pass
    
    print("Waiting 1 second...")
    await page.sleep(1)
    
    print("\n2. Emulating physical mouse scroll DOWN...")
    await page.evaluate(
        """(() => {
            let ticks = 0;
            const interval = setInterval(() => {
                window.dispatchEvent(new WheelEvent('wheel', { deltaY: 400, bubbles: true }));
                window.scrollBy({ top: 400, behavior: 'smooth' });
                ticks++;
                if (ticks >= 10) clearInterval(interval);
            }, 50);
        })()"""
    )
    
    await page.sleep(2)
    
    print("\n3. Emulating physical mouse scroll UP...")
    await page.evaluate(
        """(() => {
            let ticks = 0;
            const interval = setInterval(() => {
                window.dispatchEvent(new WheelEvent('wheel', { deltaY: -400, bubbles: true }));
                window.scrollBy({ top: -400, behavior: 'smooth' });
                ticks++;
                if (ticks >= 10) clearInterval(interval);
            }, 50);
        })()"""
    )
    
    await page.sleep(2)
    
    print("\n--- Final Page Logs ---")
    logs = await page.evaluate("document.getElementById('log').innerText")
    print(logs)
    
    print("\nTest complete! Closing browser in 3 seconds...")
    await page.sleep(3)
    
    if hasattr(browser, "stop"):
        browser.stop()

if __name__ == "__main__":
    asyncio.run(main())
