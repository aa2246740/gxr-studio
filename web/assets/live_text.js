// 文字框边输入边生效：画面马上跟着变（apply），保存等停下来 400 ms、按回车或离开输入框时才写（save）。
// 返回 flush：要立刻存的地方（比如换标签前）可以调用，没有改动时什么都不做。

export const TYPING_WAIT_MS = 400;

export function liveText(input, { apply, save, wait = TYPING_WAIT_MS }) {
  let timer = null;
  let dirty = false;
  const flush = () => {
    clearTimeout(timer);
    timer = null;
    if (!dirty) return;
    dirty = false;
    save();
  };
  input.addEventListener('input', () => {
    apply();
    dirty = true;
    clearTimeout(timer);
    timer = setTimeout(flush, wait);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.isComposing) return; // 输入法选字时的回车不算
    e.preventDefault();
    flush();
  });
  input.addEventListener('change', flush);
  return flush;
}
