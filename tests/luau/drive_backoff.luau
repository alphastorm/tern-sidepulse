-- Audit scenarios A-G, exercised against the real window observer.
local function gaps()
	local out = {}
	for i = 2, #H.run_log do
		table.insert(out, string.format("%d", H.run_log[i].t - H.run_log[i - 1].t))
	end
	return table.concat(out, " ")
end

-- A: the coordinator cannot start; every helper answers coordinator_unavailable.
H.boot()
for _ = 1, 12 do
	H.complete(0, H.UNAVAILABLE)
	H.wait_for_run(120000)
end
print("A  helper gaps (ms) while the coordinator is unavailable:", gaps())
local last_gap = H.run_log[#H.run_log].t - H.run_log[#H.run_log - 1].t
assert(last_gap >= 30000, "backoff reached the 30 s cap, got " .. last_gap)
local warns = 0
for _, line in ipairs(H.logs) do
	if line:find("helper failing") then
		warns += 1
	end
end
assert(warns == 1, "exactly one failing warning, got " .. warns)

-- B: a shim interpreter; each register succeeds, every snapshot is fenced.
H.run_log = {}
local waits = {}
for _ = 1, 8 do
	assert(H.op(H.runs[1]) == "register", "expected a register, got " .. H.op(H.runs[1]))
	H.complete(0, H.REG_OK)
	assert(#H.runs == 1 and H.op(H.runs[1]) == "snapshot", "the activating snapshot follows at once")
	H.complete(0, H.UNKNOWN_TOKEN)
	table.insert(waits, H.wait_for_run(120000))
end
print("B  wait before each re-register after a fenced snapshot (ms):", table.concat(waits, " "))
assert(waits[1] == 0 and waits[2] == 0, "the first fences re-register immediately")
assert(waits[#waits] >= 30000, "repeated fences reach the 30 s cap, got " .. waits[#waits])

-- C: an old interpreter is replaced; an accepted snapshot clears the backoff.
H.run_log = {}
assert(H.runs[1].argv[1] == "/opt/homebrew/bin/python3", "still on the default candidate")
assert(H.runs[1].argv[2] == "-I" and H.runs[1].argv[3] == "-S" and H.runs[1].argv[4] == "-X", "isolated flags with a bytecode cache")
H.complete(3, H.TOO_OLD)
H.wait_for_run(120000)
assert(H.runs[1].argv[1] == "/opt/homebrew/bin/python3.13", "the too-old interpreter is replaced, got " .. H.runs[1].argv[1])
H.complete(0, H.REG_OK)
assert(H.op(H.runs[1]) == "snapshot")
H.complete(0, H.SNAP_OK)
print("C  interpreter after python_too_old:", H.run_log[#H.run_log].python)
local recovered_warning = false
for _, line in ipairs(H.logs) do
	if line:find("helper recovered") and line:sub(1, 4) == "warn" then
		recovered_warning = true
	end
end
assert(recovered_warning, "recovery is logged at warn level")
local gap = H.wait_for_run(10000)
assert(H.op(H.runs[1]) == "snapshot" and gap <= 5000, "steady state is one snapshot per reconciliation")

-- D: overlapping callbacks reject their own interpreter, not its healthy replacement.
H.exists["/opt/homebrew/bin/python3.12"] = true
assert(#H.runs == 1 and H.op(H.runs[1]) == "snapshot" and H.runs[1].argv[1] == "/opt/homebrew/bin/python3.13")
H.commands.doctor.run(H.cx)
assert(#H.runs == 2 and H.op(H.runs[2]) == "doctor" and H.runs[2].argv[1] == "/opt/homebrew/bin/python3.13")
H.complete(3, H.TOO_OLD)
H.complete(3, H.TOO_OLD)
H.wait_for_run(120000)
assert(H.runs[1].argv[1] == "/opt/homebrew/bin/python3.12", "a concurrent callback must not reject the healthy replacement, got " .. H.runs[1].argv[1])
print("D  interpreter after two overlapping python_too_old answers:", H.runs[1].argv[1])
local function settle()
	while #H.runs > 0 do
		H.complete(0, H.op(H.runs[1]) == "register" and H.REG_OK or H.SNAP_OK)
	end
end
settle()

-- E: a latch set outside the window drops notices before the next generation.
H.world.agents = { { pane = 5, state = "idle", kind = "agent", program = "omp", host = 0, alive = true } }
H.world.roles[5] = "omp.session"
H.exists["/fake/data/disabled"] = true
H.handlers.agent_finished({ pane = 5, result = "failed" }, H.cx)
H.advance(100)
assert(#H.runs == 0, "no helper call while the latch is set")
H.exists["/fake/data/disabled"] = nil
H.handlers.agent_state({ pane = 5, old = "working", new = "idle" }, H.cx)
H.advance(100)
assert(#H.runs == 1 and H.op(H.runs[1]) == "register", "re-registers after enable")
H.complete(0, H.REG_OK)
assert(#H.runs == 1 and H.op(H.runs[1]) == "snapshot")
local enabled_snapshot = H.payload(H.runs[1])
assert(#enabled_snapshot.observations == 1 and enabled_snapshot.observations[1].pane == 5, "pane 5 is observed")
assert(enabled_snapshot.observations[1].notice == nil, "a notice recorded while disabled is not replayed into the new generation")
print("E  activating snapshot after enable carries notice:", tostring(enabled_snapshot.observations[1].notice))
H.complete(0, H.SNAP_OK)

-- F: synchronous helper failure still toasts through the command's own cx.
H.spawn_fail = true
H.toasts = {}
H.commands.doctor.run(H.cx)
assert(#H.toasts == 1 and H.toasts[1]:find("tern-sidepulse doctor", 1, true) and H.toasts[1]:find("spawn"), "doctor toasts the synchronous failure, got " .. tostring(H.toasts[1]))
H.commands.resync.run(H.cx)
assert(#H.toasts == 2 and H.toasts[2]:find("resync") and H.toasts[2]:find("spawn"), "resync toasts too")
print("F  toasts on synchronous helper failure:", H.toasts[1], "|", H.toasts[2])
H.spawn_fail = false
H.advance(60000)
H.wait_for_run(120000)
settle()

-- G: an in-place role change with no event is noticed on the next reconciliation.
H.world.roles[5] = "claude.session"
H.wait_for_run(10000)
assert(H.op(H.runs[1]) == "snapshot")
local changed_snapshot = H.payload(H.runs[1])
assert(changed_snapshot.coverage == "complete" and #changed_snapshot.observations == 0, "a re-read role excludes the pane, got " .. #changed_snapshot.observations)
print("G  observations after the role changed in place:", #changed_snapshot.observations)
H.complete(0, H.SNAP_OK)
print("OK")
