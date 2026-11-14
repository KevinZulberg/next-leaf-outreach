"""Businesses that must never be emailed. One rule, used at every stage (search, drafts, imports, sending)."""
import re

# Names: "Smith Attorneys", "Jones & Partners Law Firm", "Legal Aid ...", "Advocates Chambers", conveyancers, notaries.
LAW_NAME = re.compile(
    r'\b(attorneys?|law\s*firm|law\s*practice|law\s*chambers|law\s*group|law\s*offices?|law|lawyers?|advocates?|'
    r'solicitors?|barristers?|notar(?:y|ies)|conveyanc\w*|legal|litigation|incorporated\s+attorneys)\b', re.I)
# Website addresses: smithattorneys.co.za, jones-law.co.za, abclegal.co.za, xyzlawgroup.co.za ...
LAW_DOMAIN = re.compile(r'(attorney|lawyer|lawfirm|lawgroup|lawinc|lawchambers|lawpractice|lawoffice|solicitor|advocate|'
                        r'conveyanc|notary|notaries|legal)', re.I)
# Page text (title or opening words): strong phrases only, so a signage shop is not caught by accident.
LAW_TEXT = re.compile(
    r'\b(law\s+firm|attorneys?\s+at\s+law|firm\s+of\s+attorneys|legal\s+practitioners?|our\s+(?:attorneys|lawyers)|'
    r'admitted\s+attorneys?|notary\s+public|conveyancing|legal\s+services|commercial\s+law|family\s+law|'
    r'labour\s+law|property\s+law|criminal\s+law|litigation)\b', re.I)


def is_law_firm(name='', email='', text='', domain=''):
    """True for law firms, attorneys, advocates, conveyancers and notaries."""
    domain = (domain or (email.rsplit('@', 1)[-1] if '@' in (email or '') else '')).lower()
    labels = re.split(r'[.\-]', domain)
    return bool((name and LAW_NAME.search(name)) or (domain and (LAW_DOMAIN.search(domain) or 'law' in labels))
                or (text and LAW_TEXT.search(text)))
