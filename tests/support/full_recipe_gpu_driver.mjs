import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const [runtime, url] = process.argv.slice(2);
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
const pin = verifyPinnedRuntime();
assert.equal(pin.playwrightVersion, '1.61.1');
assert.equal(pin.chromiumRevision, '1228');
const browser = await launchPinnedChromium({ args: [
  '--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-webgpu-adapter=swiftshader',
] });
try {
  const page = await browser.newPage();
  await page.goto(url);
  await page.waitForFunction(() => globalThis.__fullRecipeResult, null, { timeout: 120000 });
  const result = await page.evaluate(() => globalThis.__fullRecipeResult);
  assert.equal(result.error, undefined, result.error);
  console.log(JSON.stringify({ ...result, pin }));
} finally {
  await browser.close();
}
