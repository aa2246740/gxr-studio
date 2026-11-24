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

async function main(): Promise<void> {
  if (!(root instanceof HTMLElement)) return;
  if (import.meta.env.DEV && wantsDev) {
    const { boot } = await import('./ui/devshell');
    await boot(root, pageErrors);
    return;
  }
  const { bootApp } = await import('./app/boot');
  await bootApp(root, pageErrors);
}

void main().catch((err: unknown) => {
  pageErrors.push(err instanceof Error ? err.message : String(err));
});
