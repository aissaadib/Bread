//! Windows Credential Manager storage for the user's own Gemini API key.
//!
//! The key is never written to the repository, the SQLite database, the
//! frontend, a log, a file on disk or a command line. It is stored as a generic
//! credential under `TARGET`, read only when a cloud request is starting, and
//! written to the child worker process through its stdin pipe.

#[cfg(windows)]
mod platform {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;
    use std::ptr;

    const CRED_TYPE_GENERIC: u32 = 1;
    const CRED_PERSIST_LOCAL_MACHINE: u32 = 2;

    #[repr(C)]
    struct FileTime {
        low: u32,
        high: u32,
    }

    #[repr(C)]
    struct CredentialW {
        flags: u32,
        type_: u32,
        target_name: *mut u16,
        comment: *mut u16,
        last_written: FileTime,
        credential_blob_size: u32,
        credential_blob: *mut u8,
        persist: u32,
        attribute_count: u32,
        attributes: *mut std::ffi::c_void,
        target_alias: *mut u16,
        user_name: *mut u16,
    }

    #[link(name = "advapi32")]
    extern "system" {
        fn CredWriteW(credential: *const CredentialW, flags: u32) -> i32;
        fn CredReadW(target: *const u16, type_: u32, flags: u32, credential: *mut *mut CredentialW) -> i32;
        fn CredDeleteW(target: *const u16, type_: u32, flags: u32) -> i32;
        fn CredFree(buffer: *mut std::ffi::c_void);
    }

    fn wide(value: &str) -> Vec<u16> {
        OsStr::new(value).encode_wide().chain(std::iter::once(0)).collect()
    }

    pub fn store(target: &str, secret: &str) -> Result<(), String> {
        let mut target_wide = wide(target);
        let mut blob = secret.as_bytes().to_vec();
        let credential = CredentialW {
            flags: 0,
            type_: CRED_TYPE_GENERIC,
            target_name: target_wide.as_mut_ptr(),
            comment: ptr::null_mut(),
            last_written: FileTime { low: 0, high: 0 },
            credential_blob_size: blob.len() as u32,
            credential_blob: blob.as_mut_ptr(),
            persist: CRED_PERSIST_LOCAL_MACHINE,
            attribute_count: 0,
            attributes: ptr::null_mut(),
            target_alias: ptr::null_mut(),
            user_name: ptr::null_mut(),
        };
        let written = unsafe { CredWriteW(&credential, 0) };
        // Scrub the plaintext copy we handed to the API.
        for byte in blob.iter_mut() {
            *byte = 0;
        }
        if written == 0 {
            return Err("Windows Credential Manager rejected the API key.".to_string());
        }
        Ok(())
    }

    pub fn load(target: &str) -> Result<Option<String>, String> {
        let target_wide = wide(target);
        let mut raw: *mut CredentialW = ptr::null_mut();
        let found = unsafe { CredReadW(target_wide.as_ptr(), CRED_TYPE_GENERIC, 0, &mut raw) };
        if found == 0 {
            return Ok(None);
        }
        if raw.is_null() {
            return Ok(None);
        }
        let mut bytes = Vec::new();
        unsafe {
            let credential = &*raw;
            if !credential.credential_blob.is_null() && credential.credential_blob_size > 0 {
                bytes.extend_from_slice(std::slice::from_raw_parts(
                    credential.credential_blob,
                    credential.credential_blob_size as usize,
                ));
            }
            CredFree(raw as *mut std::ffi::c_void);
        }
        match String::from_utf8(bytes) {
            Ok(value) if !value.trim().is_empty() => Ok(Some(value.trim().to_string())),
            Ok(_) => Ok(None),
            Err(_) => Err("The stored API key could not be decoded.".to_string()),
        }
    }

    pub fn delete(target: &str) -> Result<bool, String> {
        let target_wide = wide(target);
        let removed = unsafe { CredDeleteW(target_wide.as_ptr(), CRED_TYPE_GENERIC, 0) };
        if removed == 0 {
            return Ok(false);
        }
        Ok(true)
    }
}

#[cfg(not(windows))]
mod platform {
    pub fn store(_target: &str, _secret: &str) -> Result<(), String> {
        Err("Secure credential storage is only available on Windows.".into())
    }

    pub fn load(_target: &str) -> Result<Option<String>, String> {
        Err("Secure credential storage is only available on Windows.".into())
    }

    pub fn delete(_target: &str) -> Result<bool, String> {
        Err("Secure credential storage is only available on Windows.".into())
    }
}

pub const TARGET: &str = "Luna/GeminiApiKey";

pub fn store_gemini_key(key: &str) -> Result<(), String> {
    let trimmed = key.trim();
    if trimmed.len() < 20 {
        return Err("That API key looks too short. Paste the whole key from Google AI Studio.".into());
    }
    if trimmed.chars().any(char::is_whitespace) {
        return Err("API keys do not contain spaces. Paste the key again.".into());
    }
    platform::store(TARGET, trimmed)
}

pub fn load_gemini_key() -> Result<Option<String>, String> {
    platform::load(TARGET)
}

pub fn delete_gemini_key() -> Result<bool, String> {
    platform::delete(TARGET)
}

pub fn has_gemini_key() -> bool {
    matches!(load_gemini_key(), Ok(Some(_)))
}
