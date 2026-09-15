import asyncio
import csv
import html
import re
import sys
import time
from collections import defaultdict, Counter
import httpx
from bs4 import BeautifulSoup

SITEMAP_URL = "https://mywhooshinfo.com/sitemap.xml"
BASE_URL = "https://mywhooshinfo.com"
CONCURRENCY = 20
OUTPUT_CSV_ALL = "mywhoosh_workouts.csv"
OUTPUT_CSV_UNIQUE = "mywhoosh_workouts_unique.csv"

ZONE_CLASS_MAP = {
    'pz-0': 'Free Ride',
    'pz-1': 'Z1 - Recovery',
    'pz-2': 'Z2 - Endurance',
    'pz-3': 'Z3 - Tempo',
    'pz-4': 'Z4 - Threshold',
    'pz-5': 'Z5 - VO2Max',
    'pz-6': 'Z6 - Anaerobic',
    'freeride': 'Free Ride'
}

ZONE_FILTER_MAP = {
    0: 'Free ride',
    1: 'Z1 - Recovery',
    2: 'Z2 - Endurance',
    3: 'Z3 - Tempo',
    4: 'Z4 - Threshold',
    5: 'Z5 - Vo2Max',
    6: 'Z6 - Anaerobic'
}

def parse_duration_to_seconds(dur_str: str) -> int:
    """Parses duration string like '48:20' or '01:30:00' to seconds."""
    if not dur_str:
        return 0
    parts = dur_str.strip().split(':')
    try:
        parts = [int(p) for p in parts]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        elif len(parts) == 2:
            return parts[0] * 60 + parts[1]
        elif len(parts) == 1:
            return parts[0] * 60
    except ValueError:
        return 0
    return 0

def parse_steps(soup: BeautifulSoup) -> str:
    """Extracts structured interval steps from the page."""
    step_list = soup.select_one('.workout-step-list')
    if not step_list:
        return ''
    
    steps = []
    for child in step_list.children:
        if not hasattr(child, 'name') or not child.name:
            continue
        repeat_badge = child.find('span', class_='step')
        if repeat_badge:
            mult = repeat_badge.text.strip()
            sub_steps = []
            for s in child.find_all('div', class_='workout-step'):
                txt = re.sub(r'\s+', ' ', s.text).strip()
                if txt:
                    sub_steps.append(txt)
            if sub_steps:
                steps.append(f"{mult} [{', '.join(sub_steps)}]")
        elif 'workout-step' in child.get('class', []):
            txt = re.sub(r'\s+', ' ', child.text).strip()
            if txt:
                steps.append(txt)
    return ' | '.join(steps)

def parse_workout_html(html_text: str, url: str, site_zone_tags: set) -> dict:
    """Parses a single workout HTML page and extracts all metadata."""
    soup = BeautifulSoup(html_text, 'html.parser')
    
    # Title
    h1 = soup.find('h1')
    title = h1.text.strip() if h1 else ''
    
    # Slug
    slug = url.rstrip('/').split('/')[-1]
    
    # Category from Breadcrumb
    bc_items = [li.text.strip() for li in soup.select('.breadcrumb li')]
    category = ''
    if len(bc_items) >= 3:
        category = bc_items[1]
    elif len(bc_items) == 2:
        category = bc_items[0]
    
    # Badges
    duration = ''
    tss = ''
    intensity_factor = ''
    coach = ''
    has_instructions = False
    has_rpm = False
    
    for b in soup.find_all('span', class_='badge'):
        txt = b.text.strip()
        if txt.startswith('Duration '):
            duration = txt.replace('Duration ', '').strip()
        elif txt.startswith('TSS '):
            tss = txt.replace('TSS ', '').strip()
        elif txt.startswith('IF '):
            intensity_factor = txt.replace('IF ', '').strip()
        elif 'coach ' in txt:
            coach = txt.replace('by coach ', '').strip()
        elif txt == 'Has instructions':
            has_instructions = True
        elif txt == 'Has RPM target':
            has_rpm = True

    # Numeric Workout ID from download button
    workout_id = ''
    download_btn = soup.find('a', attrs={'data-track-event-name': 'workoutDownload'})
    if download_btn and download_btn.get('data-track-event-data'):
        raw_data = download_btn.get('data-track-event-data')
        m = re.search(r'"workout"\s*:\s*(\d+)', raw_data)
        if m:
            workout_id = m.group(1)

    # Zones from SVG intervals
    svg = soup.find('svg')
    zones_found = set()
    zone_duration_weights = defaultdict(float)
    if svg:
        for elem in svg.find_all(['rect', 'path', 'stop', 'polygon']):
            cls = elem.get('class', [])
            if isinstance(cls, str):
                cls = cls.split()
            w_str = elem.get('width')
            try:
                elem_w = float(w_str) if w_str else 1.0
            except ValueError:
                elem_w = 1.0
                
            for c in cls:
                if c in ZONE_CLASS_MAP:
                    z_label = ZONE_CLASS_MAP[c]
                    zones_found.add(z_label)
                    zone_duration_weights[z_label] += elem_w

    # Merge with site zone filter tags if any
    for z in site_zone_tags:
        zones_found.add(z)
        
    # Determine Primary Zone:
    title_lower = title.lower()
    cat_lower = category.lower()
    dur_sec = parse_duration_to_seconds(duration)
    dur_min = round(dur_sec / 60.0, 1) if dur_sec else 0.0

    if dur_sec == 0 and any(w in title_lower for w in ['rest day', 'day off', 'recovery day']):
        primary_zone = 'Rest Day'
    elif 'endurance' in cat_lower:
        primary_zone = 'Z2 - Endurance'
    elif 'tempo' in cat_lower or 'sweetspot' in cat_lower:
        primary_zone = 'Z3 - Tempo'
    elif 'threshold' in cat_lower:
        primary_zone = 'Z4 - Threshold'
    elif 'vo2' in cat_lower:
        primary_zone = 'Z5 - VO2Max'
    elif 'anaerobic' in cat_lower or 'sprint' in cat_lower:
        primary_zone = 'Z6 - Anaerobic'
    elif 'recovery' in cat_lower:
        primary_zone = 'Z1 - Recovery'
    elif zone_duration_weights:
        non_z1_weights = {k: v for k, v in zone_duration_weights.items() if 'Z1' not in k and 'Free' not in k}
        if non_z1_weights:
            primary_zone = max(non_z1_weights, key=non_z1_weights.get)
        else:
            primary_zone = max(zone_duration_weights, key=zone_duration_weights.get)
    elif site_zone_tags:
        sorted_tags = sorted(list(site_zone_tags), reverse=True)
        primary_zone = sorted_tags[0]
    elif any(w in title_lower for w in ['rest', 'off']):
        primary_zone = 'Rest Day'
    else:
        primary_zone = 'Unspecified'

    # Description from col-lg-9 (below SVG)
    desc_col = soup.select_one('.col-lg-9')
    description = ''
    if desc_col:
        mt2 = desc_col.find('div', class_='mt-2')
        if mt2:
            desc_parts = []
            curr = mt2.next_sibling
            while curr:
                if hasattr(curr, 'get_text'):
                    t = curr.get_text(separator=' ', strip=True)
                else:
                    t = str(curr).strip()
                if t:
                    desc_parts.append(t)
                curr = curr.next_sibling
            description = ' '.join(desc_parts).strip()
            description = re.sub(r'\s+', ' ', description).strip()
        else:
            description = desc_col.get_text(separator=' ', strip=True)

    # Parse interval steps
    steps_str = parse_steps(soup)

    return {
        'workout_id': workout_id,
        'title': title,
        'slug': slug,
        'category': category,
        'primary_zone': primary_zone,
        'all_zones': '; '.join(sorted(list(zones_found))),
        'duration': duration,
        'duration_minutes': dur_min,
        'duration_seconds': dur_sec,
        'tss': tss,
        'intensity_factor': intensity_factor,
        'coach': coach,
        'has_instructions': has_instructions,
        'has_rpm_target': has_rpm,
        'description': description,
        'workout_steps': steps_str,
        'url': url
    }

async def fetch_zone_filter_mappings(client: httpx.AsyncClient) -> dict:
    """Queries the 7 official zone filters to map workout slugs to zones."""
    print("Fetching official site zone filter tags...", flush=True)
    slug_to_zones = defaultdict(set)
    for zid, zname in ZONE_FILTER_MAP.items():
        url = f"{BASE_URL}/workouts/?zone={zid}&do=workoutFilterForm-submit&filter=Filter"
        try:
            r = await client.get(url, timeout=20.0)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, 'html.parser')
                for a in soup.find_all('a', href=lambda h: h and '/workouts/workout/' in h):
                    slug = a['href'].rstrip('/').split('/')[-1]
                    slug_to_zones[slug].add(zname)
        except Exception as e:
            print(f"Warning: Failed to fetch zone filter {zid}: {e}", flush=True)
    print(f"Mapped official zone tags for {len(slug_to_zones)} workouts.", flush=True)
    return slug_to_zones

async def scrape_all_workouts():
    start_time = time.time()
    limits = httpx.Limits(max_keepalive_connections=CONCURRENCY, max_connections=CONCURRENCY)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
    }

    async with httpx.AsyncClient(limits=limits, headers=headers, follow_redirects=True, timeout=25.0) as client:
        # Step 1: Fetch sitemap
        print("Fetching sitemap.xml...", flush=True)
        resp = await client.get(SITEMAP_URL)
        workout_urls = [u for u in re.findall(r'<loc>(.*?)</loc>', resp.text) if '/workouts/workout/' in u]
        total_urls = len(workout_urls)
        print(f"Discovered {total_urls} workout URLs in sitemap.", flush=True)

        # Step 2: Fetch zone filter mappings
        slug_to_zones = await fetch_zone_filter_mappings(client)

        # Step 3: Fetch all workout pages concurrently
        sem = asyncio.Semaphore(CONCURRENCY)
        results = []
        completed_count = 0
        failed_urls = []

        async def fetch_and_parse(url):
            nonlocal completed_count
            slug = url.rstrip('/').split('/')[-1]
            site_tags = slug_to_zones.get(slug, set())
            
            for attempt in range(3):
                try:
                    async with sem:
                        r = await client.get(url)
                        if r.status_code == 200:
                            html_content = r.content.decode('utf-8', errors='replace')
                            data = parse_workout_html(html_content, url, site_tags)
                            completed_count += 1
                            if completed_count % 100 == 0 or completed_count == total_urls:
                                elapsed = time.time() - start_time
                                rate = completed_count / elapsed if elapsed > 0 else 0
                                print(f"Progress: {completed_count}/{total_urls} ({completed_count*100//total_urls}%) | {rate:.1f} req/s", flush=True)
                            return data
                except Exception as e:
                    if attempt == 2:
                        print(f"Error fetching {url}: {e}", flush=True)
                    await asyncio.sleep(1.0 * (attempt + 1))
            
            failed_urls.append(url)
            completed_count += 1
            return None

        print(f"Beginning concurrent scrape of {total_urls} workouts (concurrency={CONCURRENCY})...", flush=True)
        tasks = [fetch_and_parse(u) for u in workout_urls]
        parsed_items = await asyncio.gather(*tasks)
        results = [item for item in parsed_items if item is not None]

    elapsed = time.time() - start_time
    print(f"\nScrape completed in {elapsed:.1f}s. Successfully parsed: {len(results)}/{total_urls}. Failed: {len(failed_urls)}", flush=True)

    # Step 4: Write all workouts CSV
    fieldnames = [
        'workout_id',
        'title',
        'slug',
        'category',
        'primary_zone',
        'all_zones',
        'duration',
        'duration_minutes',
        'duration_seconds',
        'tss',
        'intensity_factor',
        'coach',
        'has_instructions',
        'has_rpm_target',
        'description',
        'workout_steps',
        'url'
    ]

    with open(OUTPUT_CSV_ALL, mode='w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"Saved complete catalog ({len(results)} rows) to: {OUTPUT_CSV_ALL}", flush=True)

    # Step 5: Generate deduplicated unique workouts CSV
    unique_map = {}
    for item in results:
        key = (item['title'].lower(), item['duration_seconds'], item['tss'])
        if key not in unique_map:
            new_item = dict(item)
            new_item['all_categories'] = {item['category']} if item['category'] else set()
            new_item['all_slugs'] = {item['slug']}
            unique_map[key] = new_item
        else:
            if item['category']:
                unique_map[key]['all_categories'].add(item['category'])
            unique_map[key]['all_slugs'].add(item['slug'])
            for field in ['workout_id', 'coach', 'description', 'workout_steps', 'primary_zone']:
                if not unique_map[key][field] and item[field]:
                    unique_map[key][field] = item[field]

    unique_fieldnames = [
        'workout_id',
        'title',
        'category',
        'all_categories',
        'primary_zone',
        'all_zones',
        'duration',
        'duration_minutes',
        'duration_seconds',
        'tss',
        'intensity_factor',
        'coach',
        'has_instructions',
        'has_rpm_target',
        'description',
        'workout_steps',
        'slug',
        'url'
    ]

    unique_rows = []
    for item in unique_map.values():
        row = dict(item)
        cats = sorted(list(item['all_categories']))
        row['all_categories'] = '; '.join(cats)
        non_plan = [c for c in cats if 'training plan' not in c.lower()]
        row['category'] = non_plan[0] if non_plan else (cats[0] if cats else '')
        if 'all_slugs' in row:
            del row['all_slugs']
        unique_rows.append(row)

    unique_rows.sort(key=lambda r: (r['title'].lower(), int(r['duration_seconds']) if r['duration_seconds'] else 0))

    with open(OUTPUT_CSV_UNIQUE, mode='w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=unique_fieldnames)
        writer.writeheader()
        writer.writerows(unique_rows)
    print(f"Saved deduplicated catalog ({len(unique_rows)} unique workouts) to: {OUTPUT_CSV_UNIQUE}", flush=True)

    # Summary
    print("\n" + "="*50)
    print("MYWHOOSH WORKOUT SCRAPER SUMMARY")
    print("="*50)
    print(f"Total URL entries scraped: {len(results)}")
    print(f"Total unique workouts:      {len(unique_rows)}")
    
    zone_counts = defaultdict(int)
    for r in unique_rows:
        zone_counts[r['primary_zone']] += 1
    print("\nWorkouts by Primary Zone:")
    for z, c in sorted(zone_counts.items(), key=lambda x: -x[1]):
        print(f"  {z:20s}: {c}")

    cat_counts = defaultdict(int)
    for r in results:
        cat_counts[r['category'] or 'Uncategorized'] += 1
    print("\nWorkouts by Category:")
    for c, cnt in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {c:30s}: {cnt}")
    print("="*50)

if __name__ == '__main__':
    asyncio.run(scrape_all_workouts())
