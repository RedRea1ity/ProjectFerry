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

  function clock(seconds) {
    const whole = Math.floor(seconds || 0);
    return `${String(Math.floor(whole / 60)).padStart(2, '0')}:${String(whole % 60).padStart(2, '0')}`;
  }
  function enhance(audio, status) {
    const controls = document.querySelector('#transport-template').content.firstElementChild.cloneNode(true);
    const play = controls.querySelector('.play-key'), mute = controls.querySelector('.mute-key');
    const seek = controls.querySelector('.seek'), volume = controls.querySelector('.volume');
    const lcd = controls.querySelector('.deck-lcd');
    controls.setAttribute('aria-label', audio.getAttribute('aria-label'));
    for (const control of [play, mute, seek, volume]) control.setAttribute('aria-controls', audio.id);

    function syncTime() {
      const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
      seek.disabled = !duration;
      seek.max = String(duration);
      seek.value = String(audio.currentTime);
      const now = clock(audio.currentTime);
      lcd.textContent = now;
      seek.setAttribute('aria-valuetext', `${now} / ${clock(duration)}`);
    }
    function syncPlay() {
      const playing = !audio.paused && !audio.ended;
      play.classList.toggle('playing', playing);
      play.title = playing ? '暂停' : '播放';
      play.setAttribute('aria-label', play.title);
    }
    function syncVolume() {
      const silent = audio.muted || audio.volume === 0;
      volume.value = String(Math.round(audio.volume * 100));
      volume.setAttribute('aria-valuetext', volume.value + '%');
      mute.setAttribute('aria-pressed', String(silent));
      mute.title = silent ? '取消静音' : '静音';
    }
    play.addEventListener('click', () => {
      if (!audio.paused && !audio.ended) { audio.pause(); return; }
      audio.play().catch(error => {
        if (error.name !== 'AbortError') status.textContent = '未能播放这首音频，请再试一次。';
      });
    });
    mute.addEventListener('click', () => {
      if (audio.volume === 0) { audio.volume = 0.55; audio.muted = false; }
      else audio.muted = !audio.muted;
    });
    volume.addEventListener('input', () => { audio.volume = volume.valueAsNumber / 100; audio.muted = false; syncVolume(); });
    seek.addEventListener('input', () => { if (!seek.disabled) audio.currentTime = seek.valueAsNumber; });
    for (const event of ['loadedmetadata', 'durationchange', 'timeupdate', 'seeked', 'emptied']) audio.addEventListener(event, syncTime);
    for (const event of ['play', 'pause', 'ended', 'emptied', 'error']) audio.addEventListener(event, syncPlay);
    audio.addEventListener('volumechange', syncVolume);
    syncTime(); syncPlay(); syncVolume();
    audio.after(controls);
    audio.controls = false;
    audio.hidden = true;
  }
  enhance(deck, message);
  enhance(bgm, bgmStatus);

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
