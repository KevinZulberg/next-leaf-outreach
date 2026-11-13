"""Optional research and drafting helpers for Nextleaf Outreach Studio.

Tavily searches the public web; only first-party company website pages are inspected.
OpenAI drafts are never automatically approved or sent.
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

UA = 'NextleafResearchAssistant/1.0 (business discovery; respects robots.txt)'
SEARCH_LOCK = Lock()
DEFAULT_MONTHLY_CREDITS = 1000
_CREDIT_CACHE = {'at': 0.0, 'value': None}


def monthly_credits():
    """Credits this app may use per month (TAVILY_MONTHLY_CREDITS, default 1000)."""
    try:
        return max(1, int(os.getenv('TAVILY_MONTHLY_CREDITS', DEFAULT_MONTHLY_CREDITS)))
    except ValueError:
        return DEFAULT_MONTHLY_CREDITS
EMAIL_RE = re.compile(r'\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b', re.I)
EXCLUDE = ('facebook.com', 'instagram.com', 'linkedin.com', 'youtube.com',
           'tiktok.com', 'x.com', 'twitter.com', 'yelp.', 'yellowpages.',
           'clutch.co', 'google.com', 'wikipedia.org', 'reddit.com',
           'pinterest.com', 'tripadvisor.', 'indeed.', 'glassdoor.')
CONTACT_LINK = re.compile(r'contact|get[-_\s]+in[-_\s]+touch|reach[-_\s]+us|enquir|inquir', re.I)
ABOUT_LINK = re.compile(r'about|locations', re.I)

class HTMLInfo(HTMLParser):
    def __init__(self):
        super().__init__(); self.words=[]; self.links=[]; self.title=''; self._in_title=False; self.skip=0
        self.link_labels = {}
        self._anchor = None
        self._form_has_email = False
        self._form_has_message = False
        self._in_form = False
        self.has_contact_form = False
    def handle_starttag(self, tag, attrs):
        d=dict(attrs)
        if tag in ('script','style'): self.skip+=1
        if tag=='title': self._in_title=True
        if tag=='a' and d.get('href'):
            self._anchor = d['href']
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
        if tag in ('script','style'): self.skip=max(0,self.skip-1)
        if tag=='title': self._in_title=False
        if tag == 'a': self._anchor = None
        if tag == 'form':
            self.has_contact_form |= self._form_has_email and self._form_has_message
            self._in_form = False
    def handle_data(self, data):
        if self.skip: return
        if self._in_title:self.title+=data
        if self._anchor: self.link_labels[self._anchor].append(data)
        if data.strip(): self.words.append(data.strip())

def host_is_public(host):
    if not host or '.' not in host: return False
    if host in ('localhost','localhost.localdomain') or host.endswith(('.local','.internal')):return False
    try:
        ips=socket.getaddrinfo(host,None,type=socket.SOCK_STREAM)
        if not ips:return False
        return all(ipaddress.ip_address(v[4][0]).is_global for v in ips)
    except (socket.gaierror,ValueError):return False

def allowed_url(url, domain=None):
    try:
        p=urlparse(url)
        if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443): return False
    except ValueError:
        return False
    if domain and p.hostname.lower().removeprefix('www.') != domain.lower().removeprefix('www.'):return False
    return host_is_public(p.hostname)

def read_bounded(response, limit):
    body = bytearray()
    for chunk in response.iter_content(chunk_size=8192):
        body.extend(chunk)
        if len(body) > limit:
            return None
    return body


def crawl_page(url, domain):
    if not allowed_url(url,domain):return None
    robots_url = 'https://' + urlparse(url).netloc + '/robots.txt'
    try:
        with requests.get(robots_url, timeout=6, headers={'User-Agent': UA},
                          allow_redirects=False, stream=True) as robots:
            if robots.status_code == 200:
                raw_rules = read_bounded(robots, 128_000)
                if raw_rules is None:
                    return None
                rules = RobotFileParser(robots_url)
                rules.parse(raw_rules.decode('utf-8', errors='replace').splitlines())
                if not rules.can_fetch(UA, url):
                    return None
            elif robots.status_code not in (404, 410):
                return None
    except (requests.RequestException, OSError):
        return None
    try:
        with requests.get(url, timeout=9, headers={'User-Agent': UA},
                          allow_redirects=False, stream=True) as response:
            if response.is_redirect:
                return urljoin(url, response.headers['location'])
            if response.status_code != 200 or 'text/html' not in response.headers.get('content-type', ''):
                return None
            raw = read_bounded(response, 2_000_000)
            if raw is None:
                return None
            content = raw.decode(response.encoding or 'utf-8', errors='replace')
            parser = HTMLInfo()
            parser.feed(content)
            return parser
    except (requests.RequestException, OSError, LookupError, ValueError):return None

def inspect_page(url, domain):
    seen = set()
    for _ in range(4):
        if url in seen:
            break
        seen.add(url)
        result = crawl_page(url, domain)
        if not isinstance(result, str):
            return result, url
        url = result
    return None, url


def extract_emails(parser, domain, source):
    linked_domains = set()
    for href in parser.links:
        try:
            host = urlparse(urljoin(source, href)).hostname
        except ValueError:
            continue
        if host:
            linked_domains.add(host.lower().removeprefix('www.'))
    public_roles = {'info', 'contact', 'enquiries', 'inquiries', 'sales', 'office', 'admin',
                    'hello', 'support', 'reception', 'orders', 'quotes'}
    found=[]
    mailto_text = [
        unquote(href[7:].split('?', 1)[0]) for href in parser.links if href.lower().startswith('mailto:')
    ]
    explicit_addresses = {email.lower() for email in EMAIL_RE.findall(' '.join(mailto_text))}
    public_text = ' '.join(parser.words + mailto_text)
    public_text = re.sub(r'\s*[\[(]\s*at\s*[\])]\s*', '@', public_text, flags=re.I)
    public_text = re.sub(r'\s*[\[(]\s*dot\s*[\])]\s*', '.', public_text, flags=re.I)
    for email in EMAIL_RE.findall(public_text):
        email=email.strip('.,;:()<>').lower()
        local, mail_domain = email.rsplit('@', 1)
        own_mail = mail_domain == domain or mail_domain.endswith('.' + domain)
        published_role = local in public_roles and (mail_domain in linked_domains or email in explicit_addresses)
        if (own_mail or published_role) and local not in ('example', 'noreply', 'no-reply'):
            if email not in found:found.append(email)
    return sorted(found, key=lambda email: email.rsplit('@', 1)[1] != domain)


def contact_pages(parser, homepage, original_url, domain):
    contact, about = [], []
    for href in parser.links if parser else ():
        try:
            link = urlparse(urljoin(homepage, href))._replace(fragment='')
        except ValueError:
            continue
        if link.scheme != 'https' or (link.hostname or '').lower().removeprefix('www.') != domain:
            continue
        label = ' '.join([link.path, *parser.link_labels[href]])
        if CONTACT_LINK.search(label):
            contact.append(link.geturl())
        elif ABOUT_LINK.search(label):
            about.append(link.geturl())
    candidates = [*contact, original_url, *about, urljoin(homepage, '/contact'),
                  urljoin(homepage, '/contact-us')]
    seen = {homepage.rstrip('/')}
    for candidate in candidates:
        key = candidate.rstrip('/')
        if key not in seen:
            seen.add(key)
            yield candidate


def inspect_company_site(url):
    p=urlparse(url)
    if p.scheme!='https' or not p.hostname:return {'email':'','observation':'','source':url}
    domain=p.hostname.lower().removeprefix('www.')
    if any(domain == e or domain.endswith('.'+e) for e in EXCLUDE):return {'email':'','observation':'','source':url}
    homepage='https://'+p.hostname+'/'
    parser, homepage=inspect_page(homepage,domain)
    emails=extract_emails(parser,domain,homepage) if parser else []
    source=homepage if emails else ''
    form_source = homepage if parser and parser.has_contact_form else ''
    observation = ' '.join(parser.words[:100])[:1300] if parser else ''
    observation_source = homepage if observation else ''
    if not emails:
        base = homepage if parser else 'https://'+p.hostname+'/'
        for _, link in zip(range(5), contact_pages(parser, base, p._replace(fragment='').geturl(), domain)):
            page, final_url=inspect_page(link,domain)
            if not page:continue
            if page.has_contact_form and not form_source:
                form_source = final_url
            if not observation:
                observation = ' '.join(page.words[:100])[:1300]
                observation_source = final_url if observation else ''
            emails=extract_emails(page,domain,final_url)
            if emails:source=final_url;break
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
        remaining = min(account['plan_limit'], monthly_credits()) - account['plan_usage']
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
        limit = min(account['plan_limit'], monthly_credits())
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

def find_companies(industry, location, target, max_searches, on_search=None):
    """One foreground request. No 200-site crawl at once; user can run again."""
    seen=set(); leads=[]
    phrases=[f'{industry} companies in {location} official website',
             f'{industry} manufacturers {location} company website',
             f'{industry} suppliers {location} services contact',
             f'{industry} contractors {location} company contact',
             f'{industry} businesses {location} contact us']
    for i in range(min(max_searches,5)):
        results=tavily_search(phrases[i],20)
        if on_search:on_search()
        for r in results:
            u=(r.get('url') or '').strip();p=urlparse(u)
            if p.scheme!='https' or not p.hostname:continue
            domain=p.hostname.lower().removeprefix('www.')
            if any(domain == e or domain.endswith('.'+e) for e in EXCLUDE):continue
            if domain in seen:continue
            seen.add(domain)
            title=(r.get('title') or '').split(' | ')[0].split(' - ')[0].strip()[:140]
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
