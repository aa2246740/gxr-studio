const pageErrors: string[] = [];
window.addEventListener('error', (event) => {
  pageErrors.push(event.message || '页面出错');
});
window.addEventListener('unhandledrejection', (event) => {
  const reason = event.reason;
  pageErrors.push(reason instanceof Error ? reason.message : String(reason));
});

const root = document.querySelector('#app');
const params = new URLSearchParams(location.search);
const wantsDev = document.documentElement.dataset.page === 'dev' || params.get('dev') === '1' || params.get('autotest') === '1';

if ('serviceWorker' in navigator) {
  const readBuild = () => document.documentElement.dataset.gxrBuild || '';
  const notifyHello = () => {
    const build = readBuild();
    if (build) navigator.serviceWorker.controller?.postMessage({ type: 'gxr-sw-hello', build });
  };
  navigator.serviceWorker.addEventListener('message', (event: MessageEvent<{ type?: string }>) => {
    if (event.data?.type === 'gxr-sw-ping' && event.ports?.[0]) {
      event.ports[0].postMessage({ build: readBuild() });
    }
  });
  navigator.serviceWorker.addEventListener('controllerchange', notifyHello);
  window.addEventListener('pagehide', () => {
    navigator.serviceWorker.controller?.postMessage({ type: 'gxr-sw-bye' });
  });
  notifyHello();
}

function shouldRegisterSw(): boolean {
  if (!import.meta.env.PROD || wantsDev || !('serviceWorker' in navigator)) return false;
  const swParam = params.get('sw');
  if (swParam === '0') return false;
  if (swParam === '1') return true;
  if ((window as unknown as { __GXR_SW__?: boolean }).__GXR_SW__ === true) return true;
  return !navigator.webdriver;
}

function registerSw(): void {
  if (!shouldRegisterSw()) return;
  const base = import.meta.env.BASE_URL || '/';
  const swUrl = new URL(`${base}sw.js`, location.href).href;
  const scope = new URL(base, location.href).href;
  const run = () => {
    void navigator.serviceWorker
      .register(swUrl, { scope, updateViaCache: 'none' })
      .catch(() => undefined);
  };
  if (document.readyState === 'complete') {
    window.setTimeout(run, 0);
  } else {
    window.addEventListener('load', () => window.setTimeout(run, 0), { once: true });
  }
}

async function main(): Promise<void> {
  if (!(root instanceof HTMLElement)) return;
  if (import.meta.env.DEV && wantsDev) {
    const { boot } = await import('./ui/devshell');
    await boot(root, pageErrors);
    return;
  }
  const { bootApp } = await import('./app/boot');
  await bootApp(root, pageErrors);
  registerSw();
}

void main().catch((err: unknown) => {
  pageErrors.push(err instanceof Error ? err.message : String(err));
});
