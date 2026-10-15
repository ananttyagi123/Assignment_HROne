1. **Indexes.** Startup creates unique employee-code and `(emp_code, date)` indexes for identity and race safety.
   Attendance date/employee, employee/punch-in, and status/date indexes support listing and punch-out. Employee
   join-date and department/join-date indexes support headcount matching. I rejected a work-hours index: analytics
   aggregate work hours, but never search or sort by them.
2. **Punch-in race.** Each request inserts directly. MongoDB's unique key allows one insert for an employee and
   attendance date; competing inserts raise `DuplicateKeyError`, which the API translates to 409.
3. **Ties.** The leaderboard ranks with MongoDB `$rank`, then filters by rank. Employees tied at the limit's rank are
   all returned, with employee code providing stable display order.
4. **Headcount.** Department summary starts from employees joined by the period end, then looks up that month's logs.
   Employees with no logs remain in the aggregation and count toward headcount.
5. **Scale and rounding.** At 100 times the data, I would move repeated monthly analytics to maintained summaries and
   measure index and pipeline costs before choosing a shard key. Trend moving averages use unrounded daily rates; the
   API applies half-up rounding to four decimals only to the displayed rate and average.
