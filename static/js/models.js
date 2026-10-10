// アクティブなTOCリンクをハイライト
const sections = document.querySelectorAll('[id]');
const tocLinks = document.querySelectorAll('.toc a');
function updateToc() {
  let current = '';
  sections.forEach(s => {
    if (window.scrollY + 100 >= s.offsetTop) current = s.id;
  });
  tocLinks.forEach(a => {
    a.classList.toggle('active', a.getAttribute('href') === '#' + current);
  });
}
window.addEventListener('scroll', updateToc);
updateToc();

// 用語の吹き出し（guide.html の .term::after）は用語の左端から右へ伸びる。本文の右端を越える用語だけ
// .tip-flip で右端揃えにして、切れずに読めるようにする（#888）。外寸は CSS から読む——::after は
// * の box-sizing を受けないので、max-width に余白と枠線を足したものが最大の外寸。
// 狭い本文では右端揃えでも左端を越えるので、外寸を本文の幅までに抑え（--tip-room）、越える分だけ
// 右へ戻す（--tip-shift）。ホバーとフォーカス（Tab・タップ）の両方で開くので両方で測る（#893）
function placeTip(term) {
  const box = (term.closest('.content') || document.documentElement).getBoundingClientRect();
  const tip = getComputedStyle(term, '::after');
  const frame = ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth']
    .reduce((sum, prop) => sum + parseFloat(tip[prop]), 0);
  term.style.setProperty('--tip-room', `${box.width - frame}px`);
  const tipWidth = parseFloat(tip.maxWidth) + frame;  // tip は live なので --tip-room を反映した値
  const rect = term.getBoundingClientRect();
  const flip = rect.left + tipWidth > box.right;
  term.classList.toggle('tip-flip', flip);
  term.style.setProperty('--tip-shift', `${flip ? Math.max(0, box.left + tipWidth - rect.right) : 0}px`);
}
['mouseover', 'focusin'].forEach(type => document.addEventListener(type, e => {
  const term = e.target.closest('.term');
  if (term) placeTip(term);
}));
