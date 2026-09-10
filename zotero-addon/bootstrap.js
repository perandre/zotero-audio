var licenseScope;
var feedAPI;

async function startup({ rootURI }) {
  await Zotero.initializationPromise;
  licenseScope = { Zotero, IOUtils, PathUtils, Services,
    Cc: Components.classes, Ci: Components.interfaces };
  Services.scriptloader.loadSubScript(rootURI + "license-status.js", licenseScope);
  Services.scriptloader.loadSubScript(rootURI + "feed-api.js", licenseScope);
  feedAPI = licenseScope.AudioFeedAPI.create(Zotero);
  feedAPI.start();
  Zotero.AudioLicenseStatus = licenseScope.AudioLicenseStatus;
  Zotero.AudioLicenseStatus.start();
}

function shutdown() {
  feedAPI?.stop();
  feedAPI = null;
  if (licenseScope) {
    licenseScope.AudioLicenseStatus.stop();
    delete Zotero.AudioLicenseStatus;
    licenseScope = null;
  }
}

function install() {}
function uninstall() {}
