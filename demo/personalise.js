/* Nextleaf demo: show the visitor's own business name.
 *
 * The email link looks like  https://nextleaf.co.za/demo?c=Albion+Sections
 * This script reads the name (c=), remembers it while they click around, and
 * shows it wherever the demo shows its sample workshop name.
 *
 * Add to EVERY demo page, just before </body>:
 *   <script src="static/js/personalise.js" defer></script>
 *
 * Safe by design: it only ever writes plain text (never HTML), keeps letters,
 * numbers and a few punctuation marks, and cuts the name to 60 characters.
 * If there is no name in the link it does nothing.
 */
(function () {
  // The sample workshop names the demo uses today. Add the current one here.
  var SAMPLE_NAMES = ['Nextleaf Studio', 'Ironwood Fabrication'];

  function clean(text) {
    return (text || '')
      .replace(/[^\p{L}\p{N} &'.,()\/+-]/gu, '')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 60);
  }

  var name = '';
  try {
    var fromLink = clean(new URLSearchParams(location.search).get('c'));
    if (fromLink) sessionStorage.setItem('nl-company', fromLink);
    name = fromLink || sessionStorage.getItem('nl-company') || '';
  } catch (e) { /* storage blocked: fall back to the link only */ }
  if (!name) return;

  function run() {
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    var node;
    while ((node = walker.nextNode())) {
      var text = node.nodeValue.trim();
      if (SAMPLE_NAMES.indexOf(text) !== -1) node.nodeValue = node.nodeValue.replace(text, name);
    }
    SAMPLE_NAMES.forEach(function (sample) {
      document.title = document.title.split(sample).join(name);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run); else run();
})();
