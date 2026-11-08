export async function walkSliderKeys({ call, evalIn }) {
  const press = async (key, code, keyCode) => {
    await call('Input.dispatchKeyEvent', { type: 'rawKeyDown', key, code, windowsVirtualKeyCode: keyCode });
    const during = await evalIn(`document.querySelector('.stage-labels').textContent`);
    await call('Input.dispatchKeyEvent', { type: 'keyUp', key, code, windowsVirtualKeyCode: keyCode });
    return during;
  };
  const focus = () => evalIn(`(async () => {
    document.querySelector('#tab-adjust').click();
    await new Promise((resolve) => requestAnimationFrame(resolve));
    const range = document.querySelector('#tp-adjust input[type=range]');
    range.value = '0'; range.focus(); return range.value;
  })()`);
  const selected = () => evalIn(`document.querySelector('.tooltabs [aria-selected=true]').id`);
  await focus();
  await press('c', 'KeyC', 67);
  const crop = await selected();
  await focus();
  const compare = await press('y', 'KeyY', 89);
  await press('y', 'KeyY', 89);
  await focus();
  const original = await press(' ', 'Space', 32);
  const afterRelease = await evalIn(`document.querySelector('.stage-labels').textContent`);
  await focus();
  const pinned = await press('\\', 'Backslash', 220);
  await press('\\', 'Backslash', 220);
  const native = [];
  for (const [key, keyCode] of [['ArrowLeft', 37], ['ArrowRight', 39], ['ArrowUp', 38], ['ArrowDown', 40], ['Home', 36], ['End', 35], ['PageUp', 33], ['PageDown', 34]]) {
    await focus();
    await press(key, key, keyCode);
    native.push(await evalIn(`({ key: ${JSON.stringify(key)}, value: Number(document.activeElement.value), tab: document.querySelector('.tooltabs [aria-selected=true]').id, route: location.hash })`));
  }
  const typing = [];
  for (const tag of ['input', 'textarea', 'select', 'div']) {
    await focus();
    await evalIn(`(() => { const t = document.createElement('${tag}'); t.id = 'typing-probe'; if ('${tag}' === 'div') t.contentEditable = 'true'; document.body.append(t); t.focus(); })()`);
    const labels = [];
    for (const [key, code, keyCode] of [['c', 'KeyC', 67], ['y', 'KeyY', 89], ['\\', 'Backslash', 220], [' ', 'Space', 32]]) labels.push(await press(key, code, keyCode));
    typing.push({ tag, tab: await selected(), labels });
    await evalIn(`document.querySelector('#typing-probe').remove()`);
  }
  return { crop, compare, original, afterRelease, pinned, native, typing };
}
