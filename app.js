(() => {
  const wallpaperLayer = document.querySelector('.wallpaper-layer');
  const wallpapers = [
    "url('/static/images/radha-krishna-1.jpg')",
    "url('/static/images/radha-krishna-2.jpg')",
    "url('/static/images/radha-krishna-3.jpg')"
  ];
  let wallpaperIndex = 0;
  if (wallpaperLayer) {
    const rotateWallpaper = () => {
      wallpaperLayer.style.setProperty('--wallpaper-image', wallpapers[wallpaperIndex]);
      wallpaperLayer.classList.remove('wallpaper-transition');
      requestAnimationFrame(() => wallpaperLayer.classList.add('wallpaper-transition'));
      wallpaperIndex = (wallpaperIndex + 1) % wallpapers.length;
    };
    rotateWallpaper();
    window.setInterval(rotateWallpaper, 9000);
  }

  const toast = document.querySelector('.toast');
  const showToast = (message) => {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('show');
    window.setTimeout(() => toast.classList.remove('show'), 2800);
  };

  document.querySelectorAll('.person').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.person').forEach((item) => item.classList.remove('selected'));
      button.classList.add('selected');
      showToast(`${button.dataset.person} is ready for birthday love ✨`);
    });
  });

  document.querySelectorAll('[data-action]').forEach((button) => {
    button.addEventListener('click', async () => {
      const chosen = document.querySelector('.person.selected');
      const name = chosen?.dataset.person;
      if (!name) { showToast('Pick a person first, ra babu!'); return; }
      button.disabled = true;
      button.classList.add('loading');
      try {
        const response = await fetch('/api/send-thanks', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, action: button.dataset.action })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || 'Something went silly.');
        window.location.assign(`/mail?name=${encodeURIComponent(name)}&delivery=${encodeURIComponent(data.delivery)}`);
      } catch (error) {
        showToast(error.message || 'Aiyo! Please try once more.');
        button.disabled = false;
        button.classList.remove('loading');
      }
    });
  });

  const video = document.getElementById('birthdayVideo');
  if (video) {
    const playlist = JSON.parse(video.dataset.playlist || '[]');
    const status = document.getElementById('videoStatus');
    const fade = document.querySelector('.video-fade');
    const unmute = document.getElementById('unmuteButton');
    let index = 0;
    const playAt = (nextIndex) => {
      index = nextIndex % playlist.length;
      fade?.classList.add('visible');
      window.setTimeout(() => {
        video.src = `/static/${playlist[index]}`;
        video.load();
        video.play().catch(() => showToast('Tap play to start the birthday wishes 🎬'));
        if (status) status.textContent = `Wish ${index + 1} of ${playlist.length}`;
        window.setTimeout(() => fade?.classList.remove('visible'), 220);
      }, 360);
    };
    video.addEventListener('ended', () => playAt(index + 1));
    unmute?.addEventListener('click', () => { video.muted = !video.muted; unmute.textContent = video.muted ? '🔇 Sound off' : '🔊 Sound on'; });
    video.play().catch(() => { video.muted = true; video.play().catch(() => {}); });
  }

  const chatForm = document.getElementById('chatForm');
  const funnyReplies = [
    'ఒక పని చెయ్యి ఇక్కడ నువ్వు… ముందు నీకు ఒక chai తెచ్చుకో. తర్వాత comedy plan చేద్దాం. ☕',
    'నీ problem చాలా serious… కాబట్టి నేను ఒక meme చూసి వస్తా. 😌',
    'అది కాదు బాబు, life lo twist ఉండాలి. లేకపోతే serial ఎలా అవుతుంది? 📺',
    'నీ confidence చూస్తుంటే Wi-Fi కూడా signal పెంచుకుంటోంది! 📶',
    'Aiyo! దీనికి solution: cake + sleep + one dramatic dialogue. 🎂'
  ];
  chatForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    const input = document.getElementById('chatInput');
    const windowEl = document.getElementById('chatWindow');
    const text = input.value.trim();
    if (!text) return;
    windowEl.insertAdjacentHTML('beforeend', `<div class="chat-message user"></div>`);
    windowEl.lastElementChild.textContent = text;
    input.value = '';
    window.setTimeout(() => {
      windowEl.insertAdjacentHTML('beforeend', `<div class="chat-message bot"></div>`);
      windowEl.lastElementChild.textContent = funnyReplies[Math.floor(Math.random() * funnyReplies.length)];
      windowEl.scrollTop = windowEl.scrollHeight;
    }, 400);
  });

  const noButton = document.getElementById('noButton');
  const answerZone = document.getElementById('answerZone');
  const moveNo = () => {
    if (!noButton || !answerZone) return;
    const maxX = Math.max(0, answerZone.clientWidth - noButton.offsetWidth - 12);
    const maxY = 110;
    noButton.style.transform = `translate(${Math.round(Math.random() * maxX) - maxX / 2}px, ${Math.round(Math.random() * maxY) - maxY / 2}px)`;
    noButton.textContent = ['Nope', 'Ayyo!', 'Catch me!', 'Almost!'][Math.floor(Math.random() * 4)];
  };
  noButton?.addEventListener('mouseenter', moveNo);
  noButton?.addEventListener('touchstart', (event) => { event.preventDefault(); moveNo(); }, { passive: false });

  const questions = [
    ['✨', 'Most beautiful girl is you — Yes or No?', 'I knew it. Even the moon agrees. 🌙'],
    ['🎂', 'Is birthday cake healthier when shared with me?', 'Correct answer. Science has been informed. 🍰'],
    ['👑', 'Should your smile be declared a national treasure?', 'Petition filed. Approval is obvious. ✨']
  ];
  let questionIndex = 0;
  const yesButton = document.getElementById('yesButton');
  yesButton?.addEventListener('click', () => {
    const response = document.getElementById('questionResponse');
    response.textContent = questions[questionIndex][2];
    questionIndex += 1;
    if (questionIndex < questions.length) {
      window.setTimeout(() => {
        const [emoji, text] = questions[questionIndex];
        document.getElementById('questionEmoji').textContent = emoji;
        document.getElementById('questionText').textContent = text;
        document.getElementById('questionNumber').textContent = `0${questionIndex + 1}`;
        response.textContent = '';
        noButton.style.transform = '';
      }, 900);
    } else {
      yesButton.textContent = 'You passed! 🥳';
      yesButton.disabled = true;
      noButton.style.display = 'none';
    }
  });

  document.getElementById('secretButton')?.addEventListener('click', () => {
    document.getElementById('secretNote').classList.toggle('open');
  });
})();
