"""Research helpers for Nextleaf Outreach.

Tavily searches the public web; only the company's own website pages are inspected, respecting robots.txt.
"""
import ipaddress
import json
import os
import re
import socket
import time
from contextlib import contextmanager
from html.parser import HTMLParser
from threading import Lock
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from blocklist import is_law_firm

UA = 'Mozilla/5.0 (compatible; NextleafResearchAssistant/2.0; business discovery; respects robots.txt)'
SEARCH_LOCK = Lock()
_CREDIT_CACHE = {'at': 0.0, 'value': None}


def credit_cap(plan_limit):
    """Tavily's own plan limit, optionally lowered by TAVILY_MONTHLY_CREDITS."""
    try:
        cap = int(os.getenv('TAVILY_MONTHLY_CREDITS', '') or 0)
    except ValueError:
        cap = 0
    return min(plan_limit, cap) if cap > 0 else plan_limit


EMAIL_RE = re.compile(r'[A-Z0-9._%+\-]+@[A-Z0-9\-]+(?:\.[A-Z0-9\-]+)*\.[A-Z]{2,}', re.I)
EXCLUDE = ('facebook.com', 'instagram.com', 'linkedin.com', 'youtube.com',
           'tiktok.com', 'x.com', 'twitter.com', 'yelp.', 'yellowpages.',
           'clutch.co', 'google.com', 'wikipedia.org', 'reddit.com',
           'pinterest.com', 'tripadvisor.', 'indeed.', 'glassdoor.')
CONTACT_LINK = re.compile(r'contact|kontak|get[-_\s]*in[-_\s]*touch|reach[-_\s]*us|enquir|inquir|'
                          r'quote|talk[-_\s]*to|speak[-_\s]*to|find[-_\s]*us|visit[-_\s]*us|locat', re.I)
ABOUT_LINK = re.compile(r'about|over[-_\s]*ons|who[-_\s]*we|our[-_\s]*(?:team|company|story)|team|company', re.I)
LEGAL_LINK = re.compile(r'privacy|terms|legal|paia|popia|disclaimer', re.I)
COMMON_PATHS = ('/contact', '/contact-us', '/contact-us/', '/contact.html', '/contactus', '/contact-us.html',
                '/get-in-touch', '/enquiries', '/enquiry', '/about-us', '/about', '/request-a-quote')
MAX_EXTRA_PAGES = 8

# Never treat these as the business's contact address.
JUNK_LOCALS = {'example', 'email', 'name', 'you', 'yourname', 'user', 'username', 'test', 'noreply', 'no-reply',
               'donotreply', 'do-not-reply', 'mailer-daemon', 'postmaster', 'abuse', 'webmaster', 'unsubscribe',
               'privacy', 'sentry', 'wordpress', 'root', 'hostmaster'}
JUNK_DOMAINS = {'example.com', 'example.org', 'domain.com', 'yourdomain.com', 'yoursite.com', 'email.com',
                'sentry.io', 'sentry-next.wixpress.com', 'wixpress.com', 'wix.com', 'godaddy.com', 'wordpress.com',
                'wordpress.org', 'w3.org', 'schema.org', 'google.com', 'gstatic.com', 'facebook.com', 'sentry.wixpress.com',
                'squarespace.com', 'shopify.com', 'mailchimp.com', 'cloudflare.com', 'sitebuilder.com', 'test.com',
                'mysite.com', 'company.com', 'website.com', 'yourcompany.com', 'domain.co.za', 'yourdomain.co.za'}
FILE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'ico', 'css', 'js', 'pdf', 'woff', 'woff2', 'ttf',
                   'eot', 'mp4', 'webm', 'avif', 'bmp', 'map', 'json', 'xml', 'php', 'html', 'htm'}
FREE_MAIL = {'gmail.com', 'googlemail.com', 'yahoo.com', 'yahoo.co.za', 'ymail.com', 'outlook.com', 'hotmail.com',
             'hotmail.co.za', 'live.com', 'live.co.za', 'icloud.com', 'me.com', 'telkomsa.net', 'mweb.co.za',
             'vodamail.co.za', 'webmail.co.za', 'absamail.co.za', 'iafrica.com', 'lantic.net', 'proton.me',
             'protonmail.com', 'mail.com', 'netactive.co.za', 'worldonline.co.za', 'polka.co.za', 'yebo.co.za'}
ROLE_LOCALS = ('info', 'contact', 'enquiries', 'enquiry', 'inquiries', 'sales', 'office', 'admin', 'hello', 'reception',
               'quotes', 'orders', 'accounts', 'support', 'service', 'mail', 'general', 'team')


# UK mode: only limited companies (PECR treats them as corporate subscribers), and only generic role addresses.
UK_COMPANY = re.compile(r'\b(?:ltd|limited|llp|plc)\b', re.I)
UK_PLACE = re.compile(r'united kingdom|\bengland\b|\bscotland\b|\bwales\b|northern ireland|\+44', re.I)
UK_POSTCODE = re.compile(r'\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b')


def uk_company_evidence(text):
    return bool(UK_COMPANY.search(text) and (UK_PLACE.search(text) or UK_POSTCODE.search(text)))


def uk_role_only(emails, domain):
    """Keep only generic company addresses (info@, sales@ ...) at the company's own domain."""
    keep = []
    for email in emails:
        local, host = email.split('@')
        if same_site(host, domain) and any(local == r or local.startswith(r) for r in ROLE_LOCALS):
            keep.append(email)
    return keep


class HTMLInfo(HTMLParser):
    """Collects visible words, links, forms and every place a page can hide an email."""
    def __init__(self):
        super().__init__(); self.words=[]; self.links=[]; self.title=''; self._in_title=False; self.skip=0
        self.link_labels = {}
        self._anchor = None
        self._form_has_email = False
        self._form_has_message = False
        self._in_form = False
        self.has_contact_form = False
        self.protected = []   # Cloudflare-protected addresses, decoded
        self.jsonld = []      # text of structured-data blocks
        self._in_jsonld = False
    def handle_starttag(self, tag, attrs):
        d=dict(attrs)
        if tag in ('script','style'):
            if tag == 'script' and 'ld+json' in (d.get('type') or '').lower():
                self._in_jsonld = True
            else:
                self.skip+=1
        if d.get('data-cfemail'):
            decoded = decode_cloudflare_email(d['data-cfemail'])
            if decoded: self.protected.append(decoded)
        if tag=='title': self._in_title=True
        if tag=='a' and d.get('href'):
            href = d['href']
            if 'email-protection#' in href:
                decoded = decode_cloudflare_email(href.split('#', 1)[1])
                if decoded: self.protected.append(decoded)
            self._anchor = href
            self.links.append(self._anchor)
            self.link_labels.setdefault(self._anchor, []).append(d.get('aria-label') or d.get('title') or '')
        if tag == 'form':
            self._in_form = True
            self._form_has_email = False
            self._form_has_message = False
        if self._in_form and tag == 'textarea':
            self._form_has_message = True
        if self._in_form and tag == 'input' and (
            (d.get('type') or '').lower() == 'email' or 'email' in (d.get('name') or '').lower()
        ):
            self._form_has_email = True
        if tag=='meta' and d.get('name','').lower()=='description' and d.get('content'):
            self.words.append(d['content'])
    def handle_endtag(self, tag):
        if tag == 'script' and self._in_jsonld:
            self._in_jsonld = False
        elif tag in ('script','style'): self.skip=max(0,self.skip-1)
        if tag=='title': self._in_title=False
        if tag == 'a': self._anchor = None
        if tag == 'form':
            self.has_contact_form |= self._form_has_email and self._form_has_message
            self._in_form = False
    def handle_data(self, data):
        if self._in_jsonld:
            self.jsonld.append(data)
            return
        if self.skip: return
        if self._in_title:self.title+=data
        if self._anchor: self.link_labels[self._anchor].append(data)
        if data.strip(): self.words.append(data.strip())


def decode_cloudflare_email(encoded):
    """Cloudflare 'email protection' stores an address as hex: first byte is the XOR key."""
    try:
        raw = bytes.fromhex(encoded.strip())
        key = raw[0]
        return bytes(byte ^ key for byte in raw[1:]).decode('utf-8')
    except (ValueError, IndexError, UnicodeDecodeError):
        return ''


def same_site(host, domain):
    host = (host or '').lower().removeprefix('www.')
    return host == domain or host.endswith('.' + domain)


def host_is_public(host):
    if not host or '.' not in host: return False
    if host in ('localhost','localhost.localdomain') or host.endswith(('.local','.internal')):return False
    try:
        ips=socket.getaddrinfo(host,None,type=socket.SOCK_STREAM)
        if not ips:return False
        return all(ipaddress.ip_address(v[4][0]).is_global for v in ips)
    except (socket.gaierror,ValueError):return False


def allowed_url(url, domain=None):
    """Public http(s) address on the default port, optionally limited to one site."""
    try:
        p=urlparse(url)
        default = 443 if p.scheme == 'https' else 80
        if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.port not in (None, default): return False
    except ValueError:
        return False
    if domain and not same_site(p.hostname, domain):return False
    return host_is_public(p.hostname)


def read_bounded(response, limit):
    body = bytearray()
    for chunk in response.iter_content(chunk_size=8192):
        body.extend(chunk)
        if len(body) > limit:
            return None
    return body


class Site:
    """One company website: polite fetching with a cached robots.txt."""
    def __init__(self, domain):
        self.domain = domain
        self._robots = {}

    def _get(self, url, timeout, stream=True):
        """GET without following redirects by itself, retrying once on a timeout."""
        last = None
        for _ in range(2):
            try:
                return requests.get(url, timeout=timeout, headers={'User-Agent': UA, 'Accept': 'text/html,*/*;q=0.8',
                                    'Accept-Language': 'en'}, allow_redirects=False, stream=stream)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = exc
            except (requests.RequestException, OSError):
                return None
        return None

    def robots_allow(self, url):
        """RFC 9309: missing (4xx) means allowed; unreachable (5xx/network) means do not crawl."""
        p = urlparse(url)
        origin = f'{p.scheme}://{p.netloc}'
        if origin not in self._robots:
            rules, robots_url = None, origin + '/robots.txt'
            for _ in range(4):
                if not allowed_url(robots_url, self.domain):
                    break
                response = self._get(robots_url, 8)
                if response is None:
                    rules = False
                    break
                with response:
                    if response.is_redirect and response.headers.get('location'):
                        robots_url = urljoin(robots_url, response.headers['location'])
                        continue
                    if response.status_code == 200:
                        raw = read_bounded(response, 128_000)
                        if raw is None:
                            rules = False
                        else:
                            rules = RobotFileParser(robots_url)
                            rules.parse(raw.decode('utf-8', errors='replace').splitlines())
                    elif 400 <= response.status_code < 500:
                        rules = None
                    else:
                        rules = False
                break
            self._robots[origin] = rules
        rules = self._robots[origin]
        if rules is False:
            return False
        return True if rules is None else rules.can_fetch(UA, url)

    def fetch(self, url, allow_new_domain=False):
        """Return (parser, final_url). Follows up to 4 redirects, checking every hop."""
        for _ in range(5):
            if not allowed_url(url, None if allow_new_domain else self.domain):
                return None, url
            if not self.robots_allow(url):
                return None, url
            response = self._get(url, 12)
            if response is None:
                return None, url
            with response:
                if response.is_redirect and response.headers.get('location'):
                    nxt = urljoin(url, response.headers['location'])
                    if allow_new_domain:
                        host = (urlparse(nxt).hostname or '').lower().removeprefix('www.')
                        if host and not same_site(host, self.domain) and not any(host == e or host.endswith('.' + e) for e in EXCLUDE):
                            self.domain = host   # the company moved: follow it
                    url = nxt
                    continue
                kind = response.headers.get('content-type', '').lower()
                if response.status_code != 200 or (kind and 'html' not in kind):
                    return None, url
                raw = read_bounded(response, 2_000_000)
                if raw is None:
                    return None, url
                try:
                    content = raw.decode(response.encoding or 'utf-8', errors='replace')
                except LookupError:
                    content = raw.decode('utf-8', errors='replace')
                parser = HTMLInfo()
                try:
                    parser.feed(content)
                except Exception:
                    return None, url
                return parser, url
        return None, url


def clean_candidate(email):
    email = email.strip().strip('.,;:()<>[]"\'').lower()
    if email.count('@') != 1 or len(email) > 120:
        return ''
    local, host = email.split('@')
    tld = host.rsplit('.', 1)[-1]
    if (not local or tld in FILE_EXTENSIONS or host in JUNK_DOMAINS or local in JUNK_LOCALS
            or any(host.endswith('.' + junk) for junk in JUNK_DOMAINS) or re.search(r'@\d+x', email)
            or local.startswith(('.', '-')) or '..' in email):
        return ''
    return email


def extract_emails(parser, domain, source):
    """Published contact addresses on a page, best first.

    Accepts: addresses at the company's own domain; any address in a mailto link, Cloudflare-protected
    block or structured data on the page; and free-mail (gmail, mweb ...) addresses written as text.
    """
    mailto = []
    for href in parser.links:
        if href.lower().startswith('mailto:'):
            mailto.append(unquote(href[7:].split('?', 1)[0]))
    explicit = set()
    for chunk in [*mailto, *parser.protected]:
        for email in EMAIL_RE.findall(chunk):
            explicit.add(clean_candidate(email))
    text = ' '.join(parser.words + mailto + parser.jsonld)
    text = re.sub(r'\s*[\[({]\s*(?:at|@)\s*[\])}]\s*', '@', text, flags=re.I)
    text = re.sub(r'\s*[\[({]\s*dot\s*[\])}]\s*', '.', text, flags=re.I)
    candidates = []
    for found in [*parser.protected, *EMAIL_RE.findall(text)]:
        email = clean_candidate(found)
        if not email or email in [c[0] for c in candidates]:
            continue
        local, host = email.split('@')
        own = same_site(host, domain)
        if own or email in explicit or host in FREE_MAIL:
            role = any(local == r or local.startswith(r) for r in ROLE_LOCALS)
            rank = (0 if own and role else 1 if own else 2 if email in explicit else 3, local)
            candidates.append((email, rank))
    return [email for email, _ in sorted(candidates, key=lambda item: item[1])]


def contact_pages(parser, homepage, extra_urls, domain):
    """Pages worth trying for an email, most promising first (de-duplicated, capped)."""
    contact, about, legal = [], [], []
    for href in parser.links if parser else ():
        try:
            link = urlparse(urljoin(homepage, href))._replace(fragment='')
        except ValueError:
            continue
        if link.scheme not in ('https', 'http') or not same_site(link.hostname, domain):
            continue
        label = ' '.join([link.path, *parser.link_labels.get(href, [])])
        if CONTACT_LINK.search(label):
            contact.append(link.geturl())
        elif ABOUT_LINK.search(label):
            about.append(link.geturl())
        elif LEGAL_LINK.search(label):
            legal.append(link.geturl())
    paths = [urljoin(homepage, path) for path in COMMON_PATHS]
    seen = {homepage.rstrip('/')}
    count = 0
    for candidate in [*contact, *extra_urls, *paths[:4], *about, *paths[4:], *legal]:
        key = candidate.rstrip('/')
        if key not in seen and count < MAX_EXTRA_PAGES:
            seen.add(key)
            count += 1
            yield candidate


def inspect_company_site(url, extra_urls=(), snippet='', uk_only=False):
    """Look for a published company email on the site. Returns email, evidence page and status."""
    p=urlparse(url)
    empty = {'email':'','observation':'','source':url,'observation_source':'','status':'email_not_found'}
    if p.scheme not in ('https', 'http') or not p.hostname:return empty
    domain=p.hostname.lower().removeprefix('www.')
    if any(domain == e or domain.endswith('.'+e) for e in EXCLUDE):return empty
    site = Site(domain)
    parser, homepage = None, ''
    for scheme in ('https', 'http'):
        start = f'{scheme}://{p.hostname}/'
        parser, homepage = site.fetch(start, allow_new_domain=True)
        if parser:
            break
    domain = site.domain
    if not parser:
        homepage = f'https://{p.hostname}/'
    if parser and is_law_firm(parser.title, domain=domain, text=' '.join(parser.words[:80])):
        return {'email': '', 'observation': '', 'source': '', 'observation_source': '', 'status': 'law_firm'}
    seen_text = [' '.join(parser.words)] if parser else []
    emails = extract_emails(parser, domain, homepage) if parser else []
    if uk_only:
        emails = uk_role_only(emails, domain)
    source = homepage if emails else ''
    form_source = homepage if parser and parser.has_contact_form else ''
    observation = ' '.join(parser.words[:100])[:1300] if parser else ''
    observation_source = homepage if observation else ''
    extras = [u for u in extra_urls if u and same_site(urlparse(u).hostname, domain)]
    if not emails and (parser or extras):
        for link in contact_pages(parser, homepage, extras, domain):
            page, final_url = site.fetch(link)
            if not page:
                continue
            if page.has_contact_form and not form_source:
                form_source = final_url
            if not observation:
                observation = ' '.join(page.words[:100])[:1300]
                observation_source = final_url if observation else ''
            seen_text.append(' '.join(page.words))
            emails = extract_emails(page, domain, final_url)
            if uk_only:
                emails = uk_role_only(emails, domain)
            if emails:
                source = final_url
                break
    if not emails and snippet:
        # The search result's own text sometimes shows the address even when the site hides it.
        found = [e for e in (clean_candidate(x) for x in EMAIL_RE.findall(snippet)) if e and same_site(e.split('@')[1], domain)]
        if found:
            emails, source = found[:1], url
    if uk_only and emails and not uk_company_evidence(' '.join(seen_text)):
        # A UK-looking email but no sign of a UK limited company: do not use it.
        return {'email': '', 'observation': '', 'source': '', 'observation_source': '', 'status': 'filtered'}
    if uk_only and not emails:
        form_source = ''
    return {'email': emails[0] if emails else '', 'observation': observation,
            'source': source or form_source, 'observation_source': observation_source,
            'status': 'email_found' if emails else ('contact_form' if form_source else 'email_not_found')}

@contextmanager
def search_allowance():
    """Give one discovery run exclusive use of the remaining included credits."""
    key = os.getenv('TAVILY_API_KEY', '').strip()
    if not key:
        raise ValueError('TAVILY_API_KEY is missing from your .env file')
    with SEARCH_LOCK:
        response = requests.get(
            'https://api.tavily.com/usage',
            headers={'Authorization': f'Bearer {key}'},
            timeout=15,
        )
        response.raise_for_status()
        usage = response.json()
        account, api_key = usage['account'], usage['key']
        remaining = credit_cap(account['plan_limit']) - account['plan_usage']
        if api_key.get('limit') is not None:
            remaining = min(remaining, api_key['limit'] - api_key['usage'])
        yield max(0, remaining)


def credit_status(max_age=600):
    """Credits used/left this month, cached so pages never wait on Tavily. None if unknown."""
    key = os.getenv('TAVILY_API_KEY', '').strip()
    if not key:
        return None
    now = time.time()
    if now - _CREDIT_CACHE['at'] < max_age:
        return _CREDIT_CACHE['value']
    value = None
    try:
        response = requests.get('https://api.tavily.com/usage',
                                headers={'Authorization': f'Bearer {key}'}, timeout=5)
        response.raise_for_status()
        usage = response.json()
        account, api_key = usage['account'], usage['key']
        limit = credit_cap(account['plan_limit'])
        used = account['plan_usage']
        if api_key.get('limit') is not None:
            limit = min(limit, api_key['limit'])
            used = max(used, api_key['usage'])
        value = {'limit': limit, 'used': used, 'remaining': max(0, limit - used)}
    except (requests.RequestException, KeyError, TypeError, ValueError):
        pass
    _CREDIT_CACHE.update(at=now if value else now - max_age + 120, value=value)
    return value


def forget_credit_cache():
    _CREDIT_CACHE['at'] = 0.0


def tavily_search(query, max_results=20):
    key=os.getenv('TAVILY_API_KEY','').strip()
    if not key:raise ValueError('TAVILY_API_KEY is missing from your .env file')
    r=requests.post('https://api.tavily.com/search',json={'api_key':key,'query':query,'search_depth':'basic','max_results':min(max_results,20),'include_answer':False},timeout=35)
    r.raise_for_status()
    return r.json().get('results',[])

def find_companies(industry, location, target, max_searches, on_search=None, phrase_index=0):
    """Search for company websites. phrase_index picks which of 5 wordings to start with."""
    seen=set(); leads=[]
    phrases=[f'{industry} companies in {location} official website',
             f'{industry} manufacturers {location} company website',
             f'{industry} suppliers {location} services contact',
             f'{industry} contractors {location} company contact',
             f'{industry} businesses {location} contact us']
    for i in range(min(max_searches,5)):
        results=tavily_search(phrases[(phrase_index + i) % len(phrases)],20)
        if on_search:on_search()
        for r in results:
            u=(r.get('url') or '').strip();p=urlparse(u)
            if p.scheme!='https' or not p.hostname:continue
            domain=p.hostname.lower().removeprefix('www.')
            if any(domain == e or domain.endswith('.'+e) for e in EXCLUDE):continue
            if domain in seen:continue
            seen.add(domain)
            title=(r.get('title') or '').split(' | ')[0].split(' - ')[0].strip()[:140]
            if is_law_firm(title, domain=domain, text=(r.get('content') or '')[:600]):continue
            leads.append({'company':title or domain,'url':'https://'+domain+'/',
                          'domain':domain,'snippet':(r.get('content') or '')[:1500],
                          'source_url':u})
            if len(leads)>=target:return leads
    return leads

def ai_draft(company, website, context, sender_details):
    key=os.getenv('OPENAI_API_KEY','').strip()
    if not key:raise ValueError('OPENAI_API_KEY is missing from .env')
    if not sender_details.strip():raise ValueError('Enter your business website and contact details first')
    data={'model':os.getenv('OPENAI_MODEL','gpt-4.1-mini'),
          'temperature':0.35,
          'response_format':{'type':'json_object'},
          'messages':[{'role':'system','content':(
              'Write a short, friendly, professional B2B outreach email for Nextleaf Digital, '
              'which builds custom business software for quotes, files, invoices, job tracking and admin. '
              'Use ONLY supported facts in the context, no invented website observations, promises, or claims '
              'about inefficiency. Say you came across the business if context is limited. '
              'Include a polite question, sender contact details and an unsubscribe-by-reply sentence. '
              'Return valid JSON with subject and body string fields only. Keep body under 140 words.')},
              {'role':'user','content':json.dumps({'company':company,'website':website,
                'public_website_context':context[:3200], 'sender_details':sender_details})}]}
    r=requests.post('https://api.openai.com/v1/chat/completions',json=data,
                    headers={'Authorization':'Bearer '+key},timeout=40)
    r.raise_for_status()
    obj=json.loads(r.json()['choices'][0]['message']['content'])
    subject=str(obj.get('subject','')).strip()[:200];body=str(obj.get('body','')).strip()[:12000]
    if not subject or not body:raise ValueError('AI returned an empty draft')
    return subject,body
