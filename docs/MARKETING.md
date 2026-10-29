# Launch copy (fill in the numbers from `results.md`, then review every example by hand)

Run: `python3 scripts/grade_sites.py scripts/sites.example.txt --key ih_yourkey` (free API key on your account page).
Anonymous use allows few scans a day; with a key the limit is higher. If it stops at the limit, run it again the next day.
Pick 3 genuine examples from `results.json` (open the report, confirm the two quotes really disagree). Never quote one you haven't checked.

## LinkedIn (personal account, post as yourself)

I scanned [N] public help centers for pages that contradict each other.

[X]% had at least one pair of pages giving different answers to the same question: different limits, different deadlines, different steps.

This matters more now. An AI support assistant reads all of those pages. When two disagree, it quotes whichever one it finds first, and the customer gets a confident wrong answer.

Example: [site] says [claim A] on one page and [claim B] on another.

The scan is automated pattern matching, so every finding needs a human look. But it is a fast way to see where your docs fight each other.

Free, no signup: https://infoharness.com  (graded report in about two minutes)

Which would you check first, your help center or your internal wiki?

## Hacker News

Title: Show HN: I graded [N] help centers for pages that contradict each other
Link: https://infoharness.com/reports  (or your results post)
First comment: Built this because AI support bots quote whichever article they retrieve first, and two articles often disagree. It crawls the public docs (obeys robots.txt), flags contradictions, duplicates, stale pages and broken links, and gives an A-F grade. Contradictions are pattern-matched (numbers and units next to the same topic), so expect some false positives, and I'd love to hear where it's wrong. [X]% of the [N] sites I tried had at least one. Free, no signup.

## Reddit (r/technicalwriting, r/SaaS, r/webdev; read each sub's self-promotion rules first)

Title: I scanned [N] public help centers and [X]% had pages that contradict each other
Body: Short version of the LinkedIn post, with the three examples and the question "How do you keep your docs consistent as the product changes?" Put the link in a comment, not the title, if the sub prefers it.

## Cold email (to a docs or support lead; send 5 a day, personal not bulk)

Subject: Two pages in your help center disagree

Hi [name],
I ran a free automated check on [site]'s help center. Two pages give different answers: [page A] says [claim A], [page B] says [claim B]. It is the kind of thing an AI assistant or a customer can quote either way.
Full report with the exact pages and quotes: [report link]
No sales pitch. If it's useful, the tool is free at infoharness.com. If something in the report is wrong, tell me and I'll fix the check.
[your name]
