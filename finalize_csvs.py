import csv
from collections import defaultdict, Counter

INPUT_ALL = "mywhoosh_workouts.csv"
OUTPUT_ALL = "mywhoosh_workouts.csv"
OUTPUT_UNIQUE = "mywhoosh_workouts_unique.csv"

def finalize():
    with open(INPUT_ALL, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        all_fieldnames = list(reader.fieldnames)
        rows = list(reader)

    # 1. Polish all rows
    for r in rows:
        title_lower = r['title'].strip().lower()
        if (title_lower in ['day off', 'recovery day'] or 'rest day' in title_lower) and (r['primary_zone'] == 'Unspecified' or not r['primary_zone'] or r['duration_seconds'] == '0'):
            r['primary_zone'] = 'Rest Day'
        # Clean any extra whitespace
        for k, v in r.items():
            if isinstance(v, str):
                r[k] = v.strip()

    # Re-save cleaned OUTPUT_ALL
    with open(OUTPUT_ALL, mode='w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=all_fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[1/2] Successfully updated {OUTPUT_ALL} ({len(rows)} entries).")

    # 2. Generate deduplicated unique CSV
    unique_map = {}
    for r in rows:
        # Group by normalized title, duration_seconds, and tss
        key = (r['title'].lower(), r['duration_seconds'], r['tss'])
        if key not in unique_map:
            item = dict(r)
            item['all_categories'] = {r['category']} if r['category'] else set()
            item['all_slugs'] = {r['slug']}
            unique_map[key] = item
        else:
            if r['category']:
                unique_map[key]['all_categories'].add(r['category'])
            unique_map[key]['all_slugs'].add(r['slug'])
            # Fill in any missing metadata from other instances
            for field in ['workout_id', 'coach', 'description', 'workout_steps', 'primary_zone']:
                if not unique_map[key][field] and r[field]:
                    unique_map[key][field] = r[field]

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
        # Default category is first non-plan category if available, otherwise first category
        non_plan = [c for c in cats if 'training plan' not in c.lower()]
        row['category'] = non_plan[0] if non_plan else (cats[0] if cats else '')
        if 'all_slugs' in row:
            del row['all_slugs']
        unique_rows.append(row)

    # Sort alphabetically by title, then duration
    unique_rows.sort(key=lambda r: (r['title'].lower(), int(r['duration_seconds']) if r['duration_seconds'] else 0))

    with open(OUTPUT_UNIQUE, mode='w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=unique_fieldnames)
        writer.writeheader()
        writer.writerows(unique_rows)
    print(f"[2/2] Successfully generated {OUTPUT_UNIQUE} ({len(unique_rows)} unique workouts).")

    # Statistics
    print("\n" + "="*60)
    print("MYWHOOSH DATASET STATISTICS")
    print("="*60)
    print(f"Total catalog entries (all URLs): {len(rows)}")
    print(f"Total unique workouts:            {len(unique_rows)}")
    
    # Primary zones
    print("\nUnique Workouts by Primary Zone:")
    pz_counter = Counter(r['primary_zone'] for r in unique_rows)
    for zone, count in pz_counter.most_common():
        pct = count / len(unique_rows) * 100
        print(f"  {zone:20s}: {count:4d}  ({pct:5.1f}%)")

    # Categories
    print("\nUnique Workouts by Main Category:")
    cat_counter = Counter(r['category'] for r in unique_rows)
    for cat, count in cat_counter.most_common():
        pct = count / len(unique_rows) * 100
        print(f"  {cat:30s}: {count:4d}  ({pct:5.1f}%)")

    # TSS ranges
    tss_vals = [float(r['tss']) for r in unique_rows if r['tss']]
    if tss_vals:
        print(f"\nTSS Stats: Min={min(tss_vals):.0f}, Max={max(tss_vals):.0f}, Avg={sum(tss_vals)/len(tss_vals):.1f}")

    # Duration ranges
    dur_vals = [float(r['duration_minutes']) for r in unique_rows if r['duration_minutes']]
    if dur_vals:
        print(f"Duration Stats (mins): Min={min(dur_vals):.0f}m, Max={max(dur_vals):.0f}m, Avg={sum(dur_vals)/len(dur_vals):.1f}m")
    print("="*60)

if __name__ == '__main__':
    finalize()
