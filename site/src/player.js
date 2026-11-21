(() => {
  const deck = document.querySelector('#deck-audio');
  const list = document.querySelector('#deck-list');
  const picker = document.querySelector('#deck-files');
  const add = document.querySelector('#deck-add');
  const message = document.querySelector('#deck-message');
  const dialog = document.querySelector('#bonus-track');
  const bgm = document.querySelector('#bonus-audio');
  const bgmStatus = document.querySelector('#bonus-audio-status');
  let resumeDeck = false;
  deck.volume = 0.55;
  bgm.volume = 0.55;

  function playDeck() {
    deck.play().catch(error => {
      if (error.name !== 'AbortError') message.textContent = '请用播放键开始，或选择浏览器支持的音频。';
    });
  }
  function selectTrack() {
    message.textContent = '';
    deck.src = list.value;
    playDeck();
  }
  list.addEventListener('change', selectTrack);
  deck.addEventListener('playing', () => { message.textContent = ''; });
  deck.addEventListener('error', () => { message.textContent = '这首音频未能播放，请检查文件格式或重新选择。'; });
  deck.addEventListener('ended', () => {
    if (list.selectedIndex + 1 < list.options.length) { list.selectedIndex++; selectTrack(); }
  });
  add.hidden = false;
  add.addEventListener('click', () => picker.click());
  picker.addEventListener('change', () => {
    const first = list.options.length;
    for (const file of picker.files) {
      list.add(new Option(file.name, URL.createObjectURL(file)));
    }
    picker.value = '';
    if (list.options.length > first) { list.selectedIndex = first; selectTrack(); }
  });
  dialog.addEventListener('bonusplay', () => {
    resumeDeck = !deck.paused && !deck.ended;
    deck.pause();
    bgm.volume = deck.volume;
    bgm.muted = deck.muted;
    bgm.currentTime = 0;
    bgmStatus.textContent = '';
    bgm.play().catch(error => {
      if (dialog.open && error.name !== 'AbortError') bgmStatus.textContent = '点播放键收听彩蛋音乐。';
    });
  });
  bgm.addEventListener('playing', () => { bgmStatus.textContent = ''; });
  dialog.addEventListener('close', () => {
    if (dialog.open) return;
    bgm.pause();
    if (resumeDeck) playDeck();
    resumeDeck = false;
  });
})();
