-- Concatenated before the real window.luau and one drive; no Tern or SidePulse is contacted.
local H = {
	now = 1000,
	timers = {},
	runs = {}, -- pending tern.process.run calls: {argv, opts, cb}
	run_log = {}, -- {t, op, python}
	logs = {},
	toasts = {},
	handlers = {},
	commands = {},
	exists = { ["/opt/homebrew/bin/python3"] = true, ["/opt/homebrew/bin/python3.13"] = true },
	canned = {},
	payloads = {}, -- every value handed to tern.json.encode, including failed attempts
	encoded = {}, -- encoded stdin -> payload
	calls = { panes = 0, tabs = 0 },
	spawn_fail = false,
}

function H.new_world()
	return {
		hosts = { { id = 0, kind = "local", state = "connected", panes = {} } },
		agents = {},
		roles = {},
		panes = {},
		tabs = {},
		fail = { panes = false, tabs = false, encode = false },
	}
end
H.world = H.new_world()

H.UNAVAILABLE = '{"ok":false,"error":"coordinator_unavailable","detail":"ENOENT"}'
H.TOO_OLD = '{"ok":false,"error":"python_too_old","detail":"requires Python 3.10+"}'
H.REG_OK = '{"ok":true,"generation":7,"epoch":"e1","notice_seconds":{"done":15,"failed":30}}'
H.UNKNOWN_TOKEN = '{"ok":false,"error":"unknown_token"}'
H.SNAP_OK = '{"ok":true,"accepted":true,"desired":"idle_ready"}'
H.canned[H.UNAVAILABLE] = { ok = false, error = "coordinator_unavailable", detail = "ENOENT" }
H.canned[H.TOO_OLD] = { ok = false, error = "python_too_old", detail = "requires Python 3.10+" }
H.canned[H.REG_OK] = { ok = true, generation = 7, epoch = "e1", notice_seconds = { done = 15, failed = 30 } }
H.canned[H.UNKNOWN_TOKEN] = { ok = false, error = "unknown_token" }
H.canned[H.SNAP_OK] = { ok = true, accepted = true, desired = "idle_ready" }

local JSON_ARRAYS = setmetatable({}, { __mode = "k" })
local JSON_ESCAPES = {
	['"'] = '\\"',
	["\\"] = "\\\\",
	["\b"] = "\\b",
	["\f"] = "\\f",
	["\n"] = "\\n",
	["\r"] = "\\r",
	["\t"] = "\\t",
}

local function encode_string(value)
	-- Tern emits raw UTF-8 and raw DEL, but escapes C0, quotes and backslashes.
	return '"' .. value:gsub('[%z\1-\31\\"]', function(char)
		return JSON_ESCAPES[char] or string.format("\\u%04x", string.byte(char))
	end) .. '"'
end

local function encode_value(value)
	local kind = type(value)
	if kind == "string" then
		return encode_string(value)
	elseif kind == "boolean" then
		return if value then "true" else "false"
	elseif kind == "number" then
		assert(value == value and math.abs(value) < math.huge, "non-finite JSON number")
		return if value % 1 == 0 then string.format("%.0f", value) else tostring(value)
	elseif kind == "table" then
		local parts = {}
		if JSON_ARRAYS[value] then
			for _, item in ipairs(value) do
				table.insert(parts, encode_value(item))
			end
			return "[" .. table.concat(parts, ",") .. "]"
		end
		for key, item in pairs(value) do
			assert(type(key) == "string", "JSON object key is not a string")
			table.insert(parts, encode_string(key) .. ":" .. encode_value(item))
		end
		return "{" .. table.concat(parts, ",") .. "}"
	end
	error("unsupported JSON value: " .. kind)
end

local function decode(text)
	local value = H.canned[text]
	if value == nil then
		error("no canned decode for " .. tostring(text))
	end
	return value
end

local tern = {
	plugin = { data = "/fake/data", dir = "/fake/plugin" },
	runtime = { os = "macos" },
	now = function()
		return H.now
	end,
	timer = function(ms, cb)
		table.insert(H.timers, { due = H.now + ms, cb = cb })
		return {}
	end,
	on = function(name, cb)
		H.handlers[name] = cb
	end,
	command = function(def)
		H.commands[def.id] = def
	end,
	log = {
		warn = function(...)
			table.insert(H.logs, "warn " .. table.concat({ ... }, " "))
		end,
		info = function(...)
			table.insert(H.logs, "info " .. table.concat({ ... }, " "))
		end,
		error = function(...)
			table.insert(H.logs, "error " .. table.concat({ ... }, " "))
		end,
	},
	fs = {
		read = function(_path, _max)
			error("no such file")
		end,
		exists = function(path)
			return H.exists[path] == true
		end,
	},
	json = {
		encode = function(value)
			table.insert(H.payloads, value)
			local fail = H.world.fail.encode
			if fail == true or (type(fail) == "function" and fail(value)) then
				error("injected encode failure")
			end
			local text = encode_value(value)
			H.encoded[text] = value
			return text
		end,
		decode = decode,
		array = function(value)
			JSON_ARRAYS[value] = true
			return value
		end,
	},
	process = {
		run = function(argv, opts, cb)
			if H.spawn_fail then
				error("spawn failed")
			end
			table.insert(H.runs, { argv = argv, opts = opts, cb = cb })
			table.insert(H.run_log, { t = H.now, op = argv[#argv - 2], python = argv[1] })
		end,
	},
}

local cx = {}
cx.agents = { list = function(_self) return H.world.agents end }
cx.hosts = { list = function(_self) return H.world.hosts end }
cx.session = {
	surface = function(_self, pane, _opts)
		local role = H.world.roles[pane]
		if role == nil then
			return nil
		end
		return { surface = { id = pane, role = role, mode = "normal" } }
	end,
	panes = function(_self)
		H.calls.panes += 1
		if H.world.fail.panes then
			error("injected panes failure")
		end
		return H.world.panes
	end,
	tabs = function(_self)
		H.calls.tabs += 1
		if H.world.fail.tabs then
			error("injected tabs failure")
		end
		return H.world.tabs
	end,
}
function cx:window_key() return "" end
function cx:toast(tone, title, body) table.insert(H.toasts, tone .. " " .. title .. " " .. tostring(body)) end
H.cx = cx

-- Fire every due timer in order, repeatedly, until none is due.
function H.advance(ms)
	local target = H.now + ms
	while true do
		local next_i, next_t = nil, nil
		for i, timer in ipairs(H.timers) do
			if timer.due <= target and (next_t == nil or timer.due < next_t) then
				next_i, next_t = i, timer.due
			end
		end
		if next_i == nil then
			break
		end
		local timer = table.remove(H.timers, next_i)
		H.now = math.max(H.now, timer.due)
		timer.cb(H.cx)
	end
	H.now = target
end

function H.op(run)
	return run.argv[#run.argv - 2]
end

function H.payload(run)
	local payload = H.encoded[run.opts.stdin]
	assert(payload ~= nil, "helper stdin was not encoded by the harness")
	return payload
end

-- Complete the oldest helper with a canned result; callbacks receive a fresh cx.
function H.complete(status, stdout, timed_out)
	local run = table.remove(H.runs, 1)
	assert(run, "no pending helper run")
	run.cb({ status = status, stdout = stdout, stderr = "", timed_out = timed_out or false }, H.cx)
	return run
end

function H.boot()
	H.advance(50)
	H.advance(75)
	assert(#H.runs == 1 and H.op(H.runs[1]) == "register", "first helper call is a register")
end

function H.wait_for_run(max_ms)
	local start = H.now
	H.advance(0)
	while #H.runs == 0 do
		assert(H.now - start < max_ms, "no helper run within " .. max_ms .. " ms")
		H.advance(1000)
	end
	return H.now - start
end
