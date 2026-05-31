import Foundation

enum AppResourceBundleLocator {
    static let bundleDirectoryName = "TimeCapsuleSMBMac_TimeCapsuleSMBApp.bundle"

    static func bundleURL(
        appBundleURL: URL = Bundle.main.bundleURL,
        resourceURL: URL? = Bundle.main.resourceURL,
        fileManager: FileManager = .default
    ) -> URL? {
        for candidate in candidateURLs(appBundleURL: appBundleURL, resourceURL: resourceURL) {
            var isDirectory: ObjCBool = false
            if fileManager.fileExists(atPath: candidate.path, isDirectory: &isDirectory), isDirectory.boolValue {
                return candidate
            }
        }
        return nil
    }

    static func candidateURLs(appBundleURL: URL, resourceURL: URL?) -> [URL] {
        var candidates: [URL] = []
        if let resourceURL {
            candidates.append(resourceURL.appendingPathComponent(bundleDirectoryName, isDirectory: true))
        }
        candidates.append(appBundleURL.appendingPathComponent("Contents/Resources", isDirectory: true)
            .appendingPathComponent(bundleDirectoryName, isDirectory: true))
        candidates.append(appBundleURL.appendingPathComponent(bundleDirectoryName, isDirectory: true))
        candidates.append(appBundleURL.deletingLastPathComponent()
            .appendingPathComponent(bundleDirectoryName, isDirectory: true))

        var seen: Set<String> = []
        return candidates.filter { url in
            let key = url.standardizedFileURL.path
            if seen.contains(key) {
                return false
            }
            seen.insert(key)
            return true
        }
    }
}

enum AppResourceBundle {
    static var bundle: Bundle {
        resolvedBundle
    }

    static var bundleURL: URL? {
        resolvedBundle.bundleURL
    }

    private static let resolvedBundle: Bundle = {
        if let url = AppResourceBundleLocator.bundleURL(),
           let bundle = Bundle(url: url) {
            return bundle
        }
        #if DEBUG
        return Bundle.module
        #else
        return Bundle.main
        #endif
    }()
}

public enum AppLaunchResourceValidation {
    public static func validate() -> String? {
        guard let bundleURL = AppResourceBundle.bundleURL else {
            return "TimeCapsuleSMB resource bundle could not be located."
        }

        let localizable = bundleURL
            .appendingPathComponent("en.lproj", isDirectory: true)
            .appendingPathComponent("Localizable.strings")
        guard FileManager.default.isReadableFile(atPath: localizable.path) else {
            return "TimeCapsuleSMB resource bundle is missing en.lproj/Localizable.strings."
        }

        guard L10n.string("screen.readiness", language: .english) == "Readiness" else {
            return "TimeCapsuleSMB localized strings did not load from the resource bundle."
        }
        return nil
    }
}
