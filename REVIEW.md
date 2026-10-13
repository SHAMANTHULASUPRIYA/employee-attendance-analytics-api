# REVIEW.md

List the defects found in the starter `app/main.py` and explain how they were addressed.

| # | Where (function / line) | What needed attention | How you'd notice it (test, input, or symptom) | How it was fixed |
|---|---|---|---|---|
| 1 | Health endpoint | Health-check behavior | Call `GET /health` | Updated the endpoint to return the expected health response |
| 2 | Employee creation | Employee validation and duplicate codes | Submit valid and invalid employee data | Added validation and duplicate-code handling |
| 3 | Employee listing | Filtering, sorting, and pagination | Call `GET /employees` with query parameters | Added the required listing behavior |
| 4 | Attendance helpers | Time zones, overnight shifts, and rounding | Test timestamps and shift calculations | Corrected time calculations |
| 5 | Punch-in | Duplicate attendance records | Punch in twice for the same employee and date | Added duplicate-key handling and relied on the unique index |
| 6 | Punch-out | Selecting the correct attendance record | Test an employee with an open attendance record | Updated the punch-out logic |
| 7 | Attendance correction | Updating attendance and recording history | Submit a correction request | Recalculated fields and recorded correction history |
| 8 | Monthly analytics | Employee-specific monthly totals | Request monthly analytics for an employee | Added monthly aggregation |
| 9 | Department summary | Including employees without attendance logs | Compare department headcount with employee records | Started the summary from employee records |
| 10 | Late-arrival leaderboard | Ranking employees with tied late-minute totals | Test employees with equal late minutes | Used ranking logic that preserves ties |
| 11 | Department trend | Complete date series and headcount calculations | Request a date range with missing attendance dates | Filled missing dates and calculated trend metrics |
| 12 | Admin explain | Explaining supported query plans | Request an explain result for a documented endpoint | Added endpoint-to-query/pipeline mapping and execution statistics |

## Areas checked that may not be defects

- The unique attendance index is necessary to prevent duplicate employee/date records.
- A leaderboard can return more employees than the requested limit when employees share the rank at the cutoff.
- Employees without attendance logs still need to count toward department headcount.
- Analytics aggregation should be checked against the required response format and edge cases.

