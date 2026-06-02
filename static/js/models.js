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
