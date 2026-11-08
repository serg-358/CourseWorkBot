document.querySelectorAll('.repair-slideshow').forEach((carousel) => {
  const slides = [...carousel.querySelectorAll('.repair-slide')];
  const dots = [...carousel.querySelectorAll('[data-slide]')];
  let index = 0;
  let timer;
  const show = (next) => {
    index = (next + slides.length) % slides.length;
    slides.forEach((slide, i) => { slide.hidden = i !== index; });
    dots.forEach((dot, i) => dot.setAttribute('aria-pressed', String(i === index)));
  };
  const schedule = () => {
    clearInterval(timer);
    if (!document.hidden) {
      timer = setInterval(() => show(index + 1), 5500);
    }
  };
  carousel.querySelector('.slide-controls').hidden = false;
  carousel.querySelectorAll('[data-direction]').forEach((button) => {
    button.addEventListener('click', () => { show(index + Number(button.dataset.direction)); schedule(); });
  });
  dots.forEach((dot) => dot.addEventListener('click', () => { show(Number(dot.dataset.slide)); schedule(); }));
  carousel.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    show(index + (event.key === 'ArrowRight' ? 1 : -1));
    schedule();
  });
  document.addEventListener('visibilitychange', schedule);
  show(0);
  schedule();
});
