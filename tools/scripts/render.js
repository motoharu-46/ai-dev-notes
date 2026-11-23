// JS描画用: node scripts/render.js <url>  → {"url","text","links":[[href,text],...]} を標準出力に出す
// この環境に同梱の Node 版 Playwright(Chromium) を使う。
const { chromium } = require('playwright');
(async () => {
  const url = process.argv[2];
  const ua = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';
  const b = await chromium.launch();
  try {
    const ctx = await b.newContext({ locale: 'ja-JP', timezoneId: 'Asia/Tokyo', userAgent: ua });
    const p = await ctx.newPage();
    await p.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
    try { await p.waitForLoadState('networkidle', { timeout: 15000 }); } catch (e) {}
    await p.waitForTimeout(1500);
    const text = await p.innerText('body');
    const links = await p.$$eval('a[href]', els => els.map(e => [e.href,
      (e.innerText || e.getAttribute('aria-label') || e.title || '').trim().slice(0, 300)]));
    process.stdout.write(JSON.stringify({ url: p.url(), title: await p.title(), text, links }));
  } catch (e) {
    process.stderr.write(String(e && e.message || e));
    process.exitCode = 1;
  } finally { await b.close(); }
})();
