const path = require("node:path");

module.exports = {
  packagerConfig: {
    asar: true,
    executableName: "WindTowerCAE",
    ignore: [
      /^\/desktop\/backend-dist($|\/)/,
      /^\/desktop\/\.pyinstaller-work($|\/)/,
      /^\/out($|\/)/,
      /^\/frontend-.*\.log$/,
      /^\/frontend\.(out|err)\.log$/,
    ],
    extraResource: [
      path.join(__dirname, "desktop", "backend-dist"),
    ],
  },
  rebuildConfig: {},
  makers: [
    {
      name: "@electron-forge/maker-squirrel",
      config: {
        name: "WindTowerCAE",
        setupExe: "WindTowerCAE-Setup.exe",
        noMsi: true,
        ...(process.env.WINDOWS_CERTIFICATE_FILE ? {
          certificateFile: process.env.WINDOWS_CERTIFICATE_FILE,
          certificatePassword: process.env.WINDOWS_CERTIFICATE_PASSWORD,
        } : {}),
      },
    },
    {
      name: "@electron-forge/maker-zip",
      platforms: ["win32"],
    },
  ],
};
