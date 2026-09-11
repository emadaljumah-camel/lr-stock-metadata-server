local LrBinding = import "LrBinding"
local LrPrefs = import "LrPrefs"
local LrView = import "LrView"
local pluginConfig = require "PluginConfig"
local JobState = require "JobState"
local prefs = LrPrefs.prefsForPlugin()

local provider = {}

local function defaultServerUrl()
    return prefs.serverUrl or pluginConfig.serverUrl or "http://127.0.0.1:8008"
end

function provider.startDialog(propertyTable)
    propertyTable.lrStockMetadataServerUrl = defaultServerUrl()
    propertyTable.lrStockMetadataJobRoot = prefs.jobRoot or JobState.defaultRoot()
    propertyTable:addObserver("lrStockMetadataServerUrl", function(properties, key, value)
        local serverUrl = properties[key]
        if serverUrl and serverUrl ~= "" then
            prefs.serverUrl = serverUrl
        end
    end)
    propertyTable:addObserver("lrStockMetadataJobRoot", function(properties, key)
        if properties[key] and properties[key] ~= "" then prefs.jobRoot = properties[key] end
    end)
end

function provider.endDialog(propertyTable)
    if propertyTable.lrStockMetadataServerUrl and propertyTable.lrStockMetadataServerUrl ~= "" then
        prefs.serverUrl = propertyTable.lrStockMetadataServerUrl
    end
    if propertyTable.lrStockMetadataJobRoot and propertyTable.lrStockMetadataJobRoot ~= "" then
        prefs.jobRoot = propertyTable.lrStockMetadataJobRoot
    end
end

function provider.sectionsForTopOfDialog(f, propertyTable)
    return {
        {
            title = "LR Stock Metadata Server",
            synopsis = propertyTable.lrStockMetadataServerUrl or defaultServerUrl(),
            f:row {
                bind_to_object = propertyTable,
                spacing = f:control_spacing(),
                f:static_text {
                    title = "Server URL",
                    width = LrView.share "label_width",
                    alignment = "right",
                },
                f:edit_field {
                    value = LrView.bind("lrStockMetadataServerUrl"),
                    immediate = true,
                    width_in_chars = 48,
                },
            },
            f:static_text {
                title = "Example: http://localhost:8008",
            },
            f:row {
                bind_to_object = propertyTable,
                spacing = f:control_spacing(),
                f:static_text { title = "Local job storage", width = LrView.share "label_width", alignment = "right" },
                f:edit_field { value = LrView.bind("lrStockMetadataJobRoot"), immediate = true, width_in_chars = 48 },
            },
        },
    }
end

return provider
