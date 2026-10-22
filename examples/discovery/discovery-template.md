# Business discovery interview guide

A 45–60 minute conversation with the owner or operations lead. Process before product: understand
how the business works today before discussing any tool. Record answers in a notes JSON file (see
`notes.example.json`) under the section key shown for each question, then ingest it with:

```sh
uv run ams-assess discover --customer <customer> --notes notes.json
```

Everything recorded here is a **customer statement**, not technical evidence, until it is
independently verified. Write down what was said, in the customer's words; do not round up,
estimate on their behalf or add numbers they did not give.

## For every problem or task mentioned, ask the follow-ups

| Prompt | Notes field |
| --- | --- |
| How often does this happen? | `frequency` |
| How much staff time does it take (per day / week)? | `staff_time` |
| Does it cause mistakes, rework or missed items? | `errors_rework` |
| Does it cost money you can put a figure on (as you see it)? | `cost` |
| How do you get around it today? | `workaround` |
| What does it affect: customers, cash flow, staff, growth? | `impact` |
| Who does it? Which tools are involved? | `owner`, `systems` |

Leave a field out when the customer does not know; a missing answer is not a failure.

## Questions

1. **What does the business do, who are your customers, and what matters most to you over the
   next 12 months?** → `business_overview`
2. **Which services or jobs bring in most of the revenue, and which would you like more of?**
   → `business_overview`
3. **Which systems do you use day to day: CRM, booking, accounting, phone, email, job or
   operations tools, spreadsheets?** → `systems_in_use`
4. **How do new enquiries reach you, and what happens to a call or message nobody answers?**
   → `enquiries_communication`
5. **How quickly do enquiries get a reply, and who replies out of hours?**
   → `enquiries_communication`
6. **Walk me through how a quote is prepared, sent and followed up.** → `sales_quoting`
7. **How do you keep customers updated before, during and after a job?** → `sales_quoting`
8. **How are bookings, jobs and staff scheduled, and what happens when a job moves?**
   → `bookings_scheduling`
9. **Which tasks are done by hand every day or week that feel repetitive?**
   → `manual_duplicated_tasks`
10. **Is the same information typed into more than one system? Which systems do not talk to each
    other?** → `data_integrations`
11. **Where do things go wrong, get delayed or pile up? What causes the most errors or
    complaints?** → `pain_points_bottlenecks`
12. **What is already automated, and are you using any AI tools today? What has worked or not?**
    → `existing_automations`
13. **If one part of the business ran itself tomorrow, which would you pick?**
    → `opportunities_constraints`
14. **What constraints should we respect: budget, timing, regulation, staff skills, tools you
    must keep?** → `opportunities_constraints`
15. **Which decisions or messages must always be approved by a person before they go out?**
    → `opportunities_constraints`

## After the interview

- Derive findings only from what was said, and cite the statements they rest on
  (`section` or `section:N`, 0-based).
- Pick the AMS service that fits the problem — AI & Automation, Business Systems, Digital Growth or
  Managed IT & Technology. Not every assessment needs AI; often a better-configured system or a
  simple process change is the right answer.
- Do not promise time savings, revenue or ROI. Figures stay "as stated" until measured.
