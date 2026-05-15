"""
The Deep Seeker - Finds the digital exhaust people leave behind
Data sources are 100% public, but the connections are not obvious.
"""

import httpx
import re
import json
from typing import List, Dict, Any
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import asyncio

class DeepSeeker:
    def __init__(self):
        self.session = None
        self.results = []
    
    async def crawl(self, person_name: str, email: str = None, username: str = None):
        """Start the deep seeker on a target"""
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as self.session:
            tasks = []
            
            # 1. Pastebin and textbin searches
            if username or email:
                tasks.append(self.search_pastebin(person_name, username or email))
                tasks.append(self.search_ghostbin(person_name))
            
            # 2. GitHub gists and code snippets
            tasks.append(self.search_github_gists(person_name, email))
            
            # 3. WHOIS history records
            tasks.append(self.search_whois_history(person_name))
            
            # 4. Forum and comment history
            tasks.append(self.search_forum_mentions(person_name))
            
            # 5. Resume and CV databases
            tasks.append(self.search_resume_databases(person_name))
            
            # 6. Disposable email patterns
            tasks.append(self.discover_email_variants(person_name))
            
            # 7. Breach correlation (via public APIs)
            tasks.append(self.check_breach_exposure(email))
            
            # 8. Social media archives (public posts only)
            tasks.append(self.search_social_archives(person_name))
            
            # Run all crawlers in parallel
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Flatten and deduplicate
            all_findings = []
            for result in results:
                if isinstance(result, list):
                    all_findings.extend(result)
            
            return self.deduplicate_findings(all_findings)
    
    async def search_pastebin(self, name: str, query: str):
        """Search Pastebin and similar text-sharing sites"""
        findings = []
        sites = [
            f"https://psbdmp.ws/api/search/{query}",  # Pastebin dumps
            f"https://pastebin.com/archive",  # Can't search directly, but can scan
        ]
        
        for url in sites:
            try:
                resp = await self.session.get(url)
                if resp.status_code == 200:
                    findings.append({
                        "type": "pastebin_dump",
                        "source": url,
                        "content_preview": resp.text[:500],
                        "timestamp": datetime.now().isoformat(),
                        "relevance": "high" if query.lower() in resp.text.lower() else "medium"
                    })
            except:
                pass
        
        return findings
    
    async def search_ghostbin(self, name: str):
        """Search ghostbin (placeholder for potential future integration)"""
        return []
    
    async def search_github_gists(self, name: str, email: str = None):
        """Search GitHub gists for code snippets containing person's info"""
        findings = []
        
        # GitHub search for gists containing the name
        search_url = f"https://api.github.com/search/code?q={name.replace(' ', '+')}+language:json+extension:json"
        headers = {"Accept": "application/vnd.github.v3+json"}
        
        try:
            resp = await self.session.get(search_url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("items", [])[:20]:
                    # Get the actual gist content
                    gist_url = item.get("url")
                    if gist_url:
                        gist_resp = await self.session.get(gist_url, headers=headers)
                        if gist_resp.status_code == 200:
                            gist_data = gist_resp.json()
                            findings.append({
                                "type": "github_gist",
                                "gist_id": gist_data.get("id"),
                                "description": gist_data.get("description"),
                                "files": list(gist_data.get("files", {}).keys()),
                                "url": gist_data.get("html_url"),
                                "timestamp": gist_data.get("created_at")
                            })
        except:
            pass
        
        return findings
    
    async def search_whois_history(self, name: str):
        """Query historical WHOIS records for domains registered by this person"""
        findings = []
        
        # Use public WHOIS history APIs
        whois_apis = [
            f"https://www.whoisds.com/whois-database/domain-search/{name}",
            f"https://securitytrails.com/list/registrant-name/{name.replace(' ', '%20')}"
        ]
        
        for url in whois_apis:
            try:
                resp = await self.session.get(url)
                if resp.status_code == 200:
                    # Parse out domains from response
                    domains = re.findall(r'[a-zA-Z0-9][a-zA-Z0-9-]{1,61}[a-zA-Z0-9]\.[a-zA-Z]{2,}', resp.text)
                    for domain in set(domains[:10]):
                        findings.append({
                            "type": "whois_domain",
                            "domain": domain,
                            "source": url,
                            "confidence": "medium"
                        })
            except:
                pass
        
        return findings
    
    async def search_forum_mentions(self, name: str):
        """Search public forums and comment sections"""
        findings = []
        
        # Public comment/search APIs (Reddit, HackerNews, etc.)
        forum_sources = [
            f"https://old.reddit.com/r/all/search.json?q={name.replace(' ', '+')}&limit=25",
            f"https://hn.algolia.com/api/v1/search?query={name.replace(' ', '+')}"
        ]
        
        for url in forum_sources:
            try:
                resp = await self.session.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    
                    if "data" in data:  # Reddit format
                        for post in data.get("data", {}).get("children", []):
                            post_data = post.get("data", {})
                            findings.append({
                                "type": "reddit_post",
                                "title": post_data.get("title"),
                                "url": f"https://reddit.com{post_data.get('permalink')}",
                                "author": post_data.get("author"),
                                "subreddit": post_data.get("subreddit"),
                                "timestamp": datetime.fromtimestamp(post_data.get("created_utc", 0)).isoformat()
                            })
                    elif "hits" in data:  # HackerNews format
                        for hit in data.get("hits", []):
                            findings.append({
                                "type": "hackernews_post",
                                "title": hit.get("title"),
                                "url": hit.get("url"),
                                "author": hit.get("author"),
                                "points": hit.get("points"),
                                "timestamp": datetime.fromtimestamp(hit.get("created_at_i", 0)).isoformat()
                            })
            except:
                pass
        
        return findings
    
    async def search_resume_databases(self, name: str):
        """Search public resume/CV databases"""
        findings = []
        
        # Public resume sources (Google can index these)
        resume_sites = [
            f"https://www.linkedin.com/in/",  # Can't scrape directly, but can detect
            f"https://www.visualcv.com/?q={name.replace(' ', '+')}",
            f"https://www.cakeresume.com/search?q={name.replace(' ', '+')}"
        ]
        
        # Use Google's custom search API if you have a key
        # Otherwise, this is a placeholder for where to look
        
        findings.append({
            "type": "resume_hint",
            "message": f"Try searching Google for '{name} resume site:linkedin.com' or check VisualCV directly",
            "urls": resume_sites
        })
        
        return findings
    
    async def discover_email_variants(self, name: str):
        """Generate possible email address patterns for a person"""
        findings = []
        
        # Common email patterns
        first, last = name.lower().split() if " " in name else (name, "")
        first_initial = first[0] if first else ""
        
        email_patterns = [
            f"{first}.{last}@",
            f"{first}{last}@",
            f"{first_initial}{last}@",
            f"{first_initial}.{last}@",
            f"{first}-{last}@",
            f"{first}_{last}@"
        ]
        
        # Common domains to check
        domains = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "protonmail.com", "icloud.com"]
        
        possible_emails = []
        for pattern in email_patterns:
            for domain in domains:
                possible_emails.append(f"{pattern}{domain}")
        
        findings.append({
            "type": "email_variants",
            "patterns": email_patterns,
            "suggested_emails": possible_emails[:10],
            "next_action": "Try these emails with Emailrep.io or Holehe"
        })
        
        return findings
    
    async def check_breach_exposure(self, email: str = None):
        """Check if email appears in known breaches (via public APIs)"""
        findings = []
        
        if not email:
            return findings
        
        # Use Have I Been Pwned API (public, rate limited)
        # Note: This requires the user's own HIBP API key in production
        # For demo, we'll just return the public endpoint
        
        findings.append({
            "type": "breach_check",
            "tool": "Have I Been Pwned",
            "url": f"https://haveibeenpwned.com/account/{email}",
            "method": "Visit this URL to check for breaches (may be blocked by corp networks)",
            "alternative": "Use Holehe command line: holehe {email}"
        })
        
        return findings
    
    async def search_social_archives(self, name: str):
        """Search archived social media posts (Twitter archives, Facebook public pages)"""
        findings = []
        
        # Wayback Machine search for social profiles
        wayback_url = f"https://web.archive.org/web/*/https://twitter.com/*{name.replace(' ', '%20')}*"
        
        findings.append({
            "type": "social_archive",
            "wayback_query": wayback_url,
            "sources": [
                "Twitter advanced search (public tweets only)",
                "Facebook public posts (via graph search)",
                "Instagram public profiles (no login required)"
            ]
        })
        
        return findings
    
    def deduplicate_findings(self, findings: List[Dict]) -> List[Dict]:
        """Remove duplicate findings based on URL or content hash"""
        seen = set()
        unique = []
        
        for f in findings:
            key = f.get("url") or f"{f.get('type')}_{f.get('source')}"
            if key not in seen:
                seen.add(key)
                unique.append(f)
        
        return unique
