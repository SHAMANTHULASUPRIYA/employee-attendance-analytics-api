# DECISIONS.md

1. **Indexes.** I created a unique index on `employees.emp_code` to prevent duplicate employee codes, and a unique compound index on `attendance_logs.emp_code` and `date` to allow only one attendance record per employee per date. The department/employee-code index supports department-based employee queries, while the `joined_on` index supports join-date calculations. The descending date/employee-code index supports attendance listing and sorting. I did not add a separate index for every analytics pipeline because unnecessary indexes increase storage and write costs.

2. **Punch-in race.** The unique attendance index is the final protection against two requests creating records for the same employee and date. If both requests attempt an insert, only one can succeed; the other must handle the duplicate-key error. Application validation alone cannot prevent this race.

3. **Ties.** The leaderboard uses MongoDB `$rank`, so employees with equal late-minute totals share the same rank. Filtering by rank means a tie at the limit can return more employees than the requested limit.

4. **Headcount.** The department summary starts from employees and groups them by department, rather than counting only attendance logs. This allows employees with no attendance records to contribute to headcount.

5. **Scaling.** For 100 times more data, I would review query plans and execution statistics, then consider pre-aggregating frequently requested analytics if live aggregation became too expensive.