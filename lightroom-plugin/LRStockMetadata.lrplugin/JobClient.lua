local LrHttp = import "LrHttp"
local json = require "json"

local JobClient = {}

local function responseStatus(headers)
    if type(headers) ~= "table" then return nil end
    local value = headers.status or headers[":status"] or headers[1]
    local code = value and tostring(value):match("%d%d%d")
    return code and tonumber(code) or nil
end

local function printable(value)
    if type(value) ~= "table" then return tostring(value or "") end
    local parts = {}
    for key, item in pairs(value) do parts[#parts + 1] = tostring(key) .. "=" .. printable(item) end
    return table.concat(parts, "; ")
end

local function decodeResponse(body, headers)
    local status = responseStatus(headers)
    if not body then return nil, "Metadata server did not return a response" end
    local ok, result = pcall(function() return json.decode(body) end)
    if not ok or type(result) ~= "table" then return nil, "Metadata server returned invalid JSON" end
    if status and status >= 400 then
        return nil, "Metadata server HTTP error " .. tostring(status) .. ": "
            .. printable(result.detail or result.error or body)
    end
    return result
end

local function baseUrl(settings)
    return tostring(settings.serverUrl or ""):gsub("/+$", "")
end

local function post(settings, path, payload)
    local body, headers = LrHttp.post(
        baseUrl(settings) .. path,
        json.encode(payload or {}),
        { { field = "Content-Type", value = "application/json" } },
        "POST",
        settings.timeoutSeconds
    )
    return decodeResponse(body, headers)
end

local function postBytes(settings, path, data, contentType)
    local body, headers = LrHttp.post(
        baseUrl(settings) .. path,
        data,
        { { field = "Content-Type", value = contentType } },
        "POST",
        settings.timeoutSeconds
    )
    return decodeResponse(body, headers)
end

local function get(settings, path)
    local body, headers = LrHttp.get(baseUrl(settings) .. path, {}, settings.timeoutSeconds)
    return decodeResponse(body, headers)
end

local function readFileBytes(path)
    local file, openError = io.open(path, "rb")
    if not file then return nil, openError end
    local bytes = file:read("*all"); file:close(); return bytes
end

function JobClient.create(settings, manifest)
    return post(settings, "/jobs", {
        client_job_id = manifest.client_job_id,
        client = "lightroom",
        plugin_version = manifest.plugin_version,
        catalog_path = manifest.catalog_path,
        photos = manifest.server_photos,
    })
end

function JobClient.uploaded(settings, jobId)
    return get(settings, "/jobs/" .. jobId .. "/uploads")
end

function JobClient.upload(settings, jobId, photoId, previewPath)
    local bytes, readError = readFileBytes(previewPath)
    if not bytes then return nil, readError end
    return postBytes(
        settings,
        "/jobs/" .. jobId .. "/photos/" .. photoId .. "/preview/binary",
        bytes,
        "image/jpeg"
    )
end

function JobClient.run(settings, jobId)
    return post(settings, "/jobs/" .. jobId .. "/run", {})
end

function JobClient.status(settings, jobId)
    return get(settings, "/jobs/" .. jobId .. "/status")
end

function JobClient.result(settings, jobId)
    return get(settings, "/jobs/" .. jobId .. "/result")
end

function JobClient.delete(settings, jobId)
    local body, headers = LrHttp.post(
        baseUrl(settings) .. "/jobs/" .. jobId,
        "",
        {},
        "DELETE",
        settings.timeoutSeconds
    )
    return decodeResponse(body, headers)
end

function JobClient.submit(settings, manifest, progress)
    local created, createError = JobClient.create(settings, manifest)
    if not created then return nil, createError end
    local jobId = created.job_id
    local uploadState, uploadError = JobClient.uploaded(settings, jobId)
    if not uploadState then return nil, uploadError end
    local uploaded = {}
    for _, photoId in ipairs(uploadState.uploaded_photo_ids or {}) do uploaded[photoId] = true end
    for index, photo in ipairs(manifest.photos) do
        if progress and progress:isCanceled() then return nil, "Upload canceled. The saved job can be resumed later." end
        if not uploaded[photo.photo_id] then
            if progress then progress:setCaption("Uploading " .. tostring(index) .. " of " .. tostring(#manifest.photos) .. ": " .. photo.filename) end
            local response, uploadOneError = JobClient.upload(settings, jobId, photo.photo_id, photo.preview_path)
            if not response then return nil, uploadOneError end
        end
    end
    return JobClient.run(settings, jobId)
end

return JobClient
