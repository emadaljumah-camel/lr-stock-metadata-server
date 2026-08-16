return {
    LrSdkVersion = 6.0,
    LrSdkMinimumVersion = 6.0,
    LrToolkitIdentifier = "com.lrstockmetadata.plugin",
    LrPluginName = "05 LR Stock Metadata",
    LrPluginInfoUrl = "https://127.0.0.1:8008",
    LrPluginInfoProvider = "PluginInfoProvider.lua",

    LrLibraryMenuItems = {
        {
            title = "Generate Stock Metadata",
            file = "GenerateStockMetadata.lua",
        },
        {
            title = "Check Job Status / Apply Results",
            file = "CheckJobStatus.lua",
        },
    },

    VERSION = { major = 1, minor = 1, revision = 0, build = 1 },
}
