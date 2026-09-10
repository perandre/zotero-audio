var licenseScope;

async function startup({ rootURI }) {
  await Zotero.initializationPromise;
  licenseScope = { Zotero, IOUtils, PathUtils, Services,
    Cc: Components.classes, Ci: Components.interfaces };
  Services.scriptloader.loadSubScript(rootURI + "license-status.js", licenseScope);
  Zotero.AudioLicenseStatus = licenseScope.AudioLicenseStatus;
  Zotero.AudioLicenseStatus.start();
}

function shutdown() {
  if (licenseScope) {
    licenseScope.AudioLicenseStatus.stop();
    delete Zotero.AudioLicenseStatus;
    licenseScope = null;
  }
}

function install() {}
function uninstall() {}
