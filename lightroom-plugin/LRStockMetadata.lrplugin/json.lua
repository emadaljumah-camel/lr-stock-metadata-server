local json = {}

local function escapeString(value)
    value = value:gsub("\\", "\\\\")
    value = value:gsub('"', '\\"')
    value = value:gsub("\n", "\\n")
    value = value:gsub("\r", "\\r")
    value = value:gsub("\t", "\\t")
    return '"' .. value .. '"'
end

local function isArray(value)
    local count = 0
    for key, _ in pairs(value) do
        if type(key) ~= "number" then return false end
        if key > count then count = key end
    end
    return true, count
end

local function encodeValue(value)
    local valueType = type(value)
    if valueType == "nil" then return "null" end
    if valueType == "boolean" then return value and "true" or "false" end
    if valueType == "number" then return tostring(value) end
    if valueType == "string" then return escapeString(value) end
    if valueType == "table" then
        local array, count = isArray(value)
        local parts = {}
        if array then
            for i = 1, count do
                parts[#parts + 1] = encodeValue(value[i])
            end
            return "[" .. table.concat(parts, ",") .. "]"
        end
        for key, item in pairs(value) do
            parts[#parts + 1] = escapeString(tostring(key)) .. ":" .. encodeValue(item)
        end
        return "{" .. table.concat(parts, ",") .. "}"
    end
    return "null"
end

function json.encode(value)
    return encodeValue(value)
end

local Parser = {}
Parser.__index = Parser

function Parser:new(text)
    return setmetatable({ text = text, pos = 1 }, self)
end

function Parser:peek()
    return self.text:sub(self.pos, self.pos)
end

function Parser:skipSpace()
    while self:peek():match("%s") do
        self.pos = self.pos + 1
    end
end

function Parser:parseString()
    self.pos = self.pos + 1
    local out = {}
    while self.pos <= #self.text do
        local ch = self:peek()
        if ch == '"' then
            self.pos = self.pos + 1
            return table.concat(out)
        end
        if ch == "\\" then
            local nextCh = self.text:sub(self.pos + 1, self.pos + 1)
            local map = { ['"'] = '"', ["\\"] = "\\", ["/"] = "/", b = "\b", f = "\f", n = "\n", r = "\r", t = "\t" }
            out[#out + 1] = map[nextCh] or nextCh
            self.pos = self.pos + 2
        else
            out[#out + 1] = ch
            self.pos = self.pos + 1
        end
    end
    error("Unterminated string")
end

function Parser:parseNumber()
    local start = self.pos
    while self:peek():match("[%d%+%-%.eE]") do
        self.pos = self.pos + 1
    end
    return tonumber(self.text:sub(start, self.pos - 1))
end

function Parser:parseArray()
    self.pos = self.pos + 1
    local out = {}
    self:skipSpace()
    if self:peek() == "]" then self.pos = self.pos + 1 return out end
    while true do
        out[#out + 1] = self:parseValue()
        self:skipSpace()
        local ch = self:peek()
        self.pos = self.pos + 1
        if ch == "]" then return out end
        if ch ~= "," then error("Expected comma in array") end
    end
end

function Parser:parseObject()
    self.pos = self.pos + 1
    local out = {}
    self:skipSpace()
    if self:peek() == "}" then self.pos = self.pos + 1 return out end
    while true do
        self:skipSpace()
        local key = self:parseString()
        self:skipSpace()
        if self:peek() ~= ":" then error("Expected colon in object") end
        self.pos = self.pos + 1
        out[key] = self:parseValue()
        self:skipSpace()
        local ch = self:peek()
        self.pos = self.pos + 1
        if ch == "}" then return out end
        if ch ~= "," then error("Expected comma in object") end
    end
end

function Parser:parseValue()
    self:skipSpace()
    local ch = self:peek()
    if ch == '"' then return self:parseString() end
    if ch == "{" then return self:parseObject() end
    if ch == "[" then return self:parseArray() end
    if ch == "t" then self.pos = self.pos + 4 return true end
    if ch == "f" then self.pos = self.pos + 5 return false end
    if ch == "n" then self.pos = self.pos + 4 return nil end
    return self:parseNumber()
end

function json.decode(text)
    local parser = Parser:new(text)
    return parser:parseValue()
end

return json

