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
        guard AppResourceBundle.bundleURL != nil else {
            return "TimeCapsuleSMB resource bundle could not be located."
        }

        guard let localizable = englishStringsURL(in: AppResourceBundle.bundle),
              FileManager.default.isReadableFile(atPath: localizable.path) else {
            return "TimeCapsuleSMB resource bundle is missing en.lproj/Localizable.strings."
        }

        // Validate resource lookup, not wording that can change during copy edits.
        let key = "screen.readiness"
        let localized = L10n.string(key, language: .english)
        guard !localized.isEmpty, localized != key else {
            return "TimeCapsuleSMB localized strings did not load from the resource bundle."
        }
        return nil
    }

    // Packaged apps copy a flat bundle; newer SwiftPM builds use Contents/Resources.
    // Bundle resolves both layouts.
    static func englishStringsURL(in bundle: Bundle) -> URL? {
        bundle.url(forResource: "Localizable", withExtension: "strings", subdirectory: nil, localization: "en")
    }
}
