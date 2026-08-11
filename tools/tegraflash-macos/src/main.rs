use log::{info, warn};
use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::sync::Arc;
use std::time::Duration;

const NVIDIA_VENDOR_ID: u16 = 0x0955;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Identity {
    vendor_id: u16,
    product_id: u16,
}

#[derive(Debug)]
struct Options {
    listen: IpAddr,
    port: u16,
    vendor_id: u16,
    product_id: Option<u16>,
    serial_number: Option<String>,
    scan_interval: Duration,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            listen: IpAddr::V4(Ipv4Addr::UNSPECIFIED),
            port: 3240,
            vendor_id: NVIDIA_VENDOR_ID,
            product_id: None,
            serial_number: None,
            scan_interval: Duration::from_millis(500),
        }
    }
}

fn parse_u16(value: &str) -> Result<u16, Box<dyn Error>> {
    let value = value.trim();
    if let Some(hex) = value
        .strip_prefix("0x")
        .or_else(|| value.strip_prefix("0X"))
    {
        Ok(u16::from_str_radix(hex, 16)?)
    } else {
        Ok(value.parse()?)
    }
}

fn read_bootrom_identity_string(
    handle: &rusb::DeviceHandle<rusb::GlobalContext>,
) -> Result<String, Box<dyn Error>> {
    const GET_DESCRIPTOR: u8 = 0x06;
    const STRING_DESCRIPTOR: u16 = 0x03;
    const BOOTROM_IDENTITY_INDEX: u16 = 0x03;

    let mut descriptor = [0u8; 130];
    let length = handle.read_control(
        0x80,
        GET_DESCRIPTOR,
        (STRING_DESCRIPTOR << 8) | BOOTROM_IDENTITY_INDEX,
        0,
        &mut descriptor,
        Duration::from_secs(1),
    )?;
    if length < 2 || descriptor[1] != STRING_DESCRIPTOR as u8 {
        return Err("invalid T234 BootROM identity descriptor".into());
    }
    let descriptor_length = usize::from(descriptor[0]).min(length);
    if descriptor_length < 4 || descriptor_length % 2 != 0 {
        return Err("malformed T234 BootROM identity descriptor".into());
    }
    let utf16: Vec<u16> = descriptor[2..descriptor_length]
        .chunks_exact(2)
        .map(|unit| u16::from_le_bytes([unit[0], unit[1]]))
        .collect();
    Ok(String::from_utf16(&utf16)?)
}

fn usage() {
    println!(
        "helm-apx-bridge - export NVIDIA recovery USB to Docker Desktop\n\
         \n\
         Usage: helm-apx-bridge [options]\n\
         \n\
           --vid <id>       USB vendor ID (default: 0x0955)\n\
           --pid <id>       optional USB product-ID filter\n\
           --serial <text>  BootROM identity override for recovery retries\n\
           --listen <addr>  listen address (default: 0.0.0.0)\n\
           --port <port>    USB/IP TCP port (default: 3240)\n\
           --scan-ms <ms>   hot-plug scan interval (default: 500)\n\
           -h, --help       show this help\n\
         \n\
         Only matching devices are exported. Merely starting the bridge reads\n\
         descriptors; recovery payload traffic begins only after a USB/IP client\n\
         explicitly attaches the device."
    );
}

fn parse_options() -> Result<Option<Options>, Box<dyn Error>> {
    let mut options = Options::default();
    let mut args = std::env::args().skip(1);

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-h" | "--help" => {
                usage();
                return Ok(None);
            }
            "--vid" => {
                options.vendor_id = parse_u16(&args.next().ok_or("--vid needs a value")?)?;
            }
            "--pid" => {
                options.product_id = Some(parse_u16(&args.next().ok_or("--pid needs a value")?)?);
            }
            "--serial" => {
                let serial_number = args.next().ok_or("--serial needs a value")?;
                if serial_number.is_empty() {
                    return Err("--serial must not be empty".into());
                }
                options.serial_number = Some(serial_number);
            }
            "--listen" => {
                options.listen = args.next().ok_or("--listen needs a value")?.parse()?;
            }
            "--port" => {
                options.port = args.next().ok_or("--port needs a value")?.parse()?;
            }
            "--scan-ms" => {
                let milliseconds: u64 = args.next().ok_or("--scan-ms needs a value")?.parse()?;
                if milliseconds < 50 {
                    return Err("--scan-ms must be at least 50".into());
                }
                options.scan_interval = Duration::from_millis(milliseconds);
            }
            _ => return Err(format!("unknown option: {arg}").into()),
        }
    }

    Ok(Some(options))
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let Some(options) = parse_options()? else {
        return Ok(());
    };

    let address = SocketAddr::new(options.listen, options.port);
    let server = Arc::new(usbip::UsbIpServer::new_simulated(Vec::new()));
    let server_task = tokio::spawn(usbip::server(address, server.clone()));
    info!("USB/IP listening on {address}");
    info!(
        "export filter {:04x}:{}",
        options.vendor_id,
        options
            .product_id
            .map(|value| format!("{value:04x}"))
            .unwrap_or_else(|| "*".to_string())
    );

    let mut exported: HashMap<String, Identity> = HashMap::new();
    let mut reported_waiting = false;

    loop {
        let mut present = HashSet::new();
        let devices = match rusb::devices() {
            Ok(devices) => devices,
            Err(error) => {
                warn!("USB enumeration failed: {error}");
                tokio::time::sleep(options.scan_interval).await;
                continue;
            }
        };

        for device in devices.iter() {
            let descriptor = match device.device_descriptor() {
                Ok(descriptor) => descriptor,
                Err(_) => continue,
            };
            if descriptor.vendor_id() != options.vendor_id
                || options
                    .product_id
                    .is_some_and(|pid| descriptor.product_id() != pid)
            {
                continue;
            }

            let bus_id = format!(
                "{}-{}-{}",
                device.bus_number(),
                device.address(),
                device.port_number()
            );
            let identity = Identity {
                vendor_id: descriptor.vendor_id(),
                product_id: descriptor.product_id(),
            };
            present.insert(bus_id.clone());

            if exported.get(&bus_id) == Some(&identity) {
                continue;
            }

            if exported.contains_key(&bus_id) {
                if server.remove_device(&bus_id).await.is_err() {
                    continue;
                }
                exported.remove(&bus_id);
            }

            let handle = match device.open() {
                Ok(handle) => handle,
                Err(error) => {
                    warn!(
                        "cannot open {:04x}:{:04x} at {bus_id}: {error}",
                        identity.vendor_id, identity.product_id
                    );
                    continue;
                }
            };
            if let Err(error) = handle.set_auto_detach_kernel_driver(true) {
                // macOS has no kernel USB driver to detach for APX and reports
                // NotSupported here. Claiming the interface below is what makes
                // bulk RCM traffic legal on both macOS and Linux.
                if error != rusb::Error::NotSupported {
                    warn!("cannot enable automatic driver detach at {bus_id}: {error}");
                }
            }
            let config = match device.active_config_descriptor() {
                Ok(config) => config,
                Err(error) => {
                    warn!("cannot read active configuration at {bus_id}: {error}");
                    continue;
                }
            };
            let mut claimed = Vec::new();
            let mut claim_failed = false;
            for interface in config.interfaces() {
                let number = interface.number();
                if claimed.contains(&number) {
                    continue;
                }
                if let Err(error) = handle.claim_interface(number) {
                    warn!("cannot claim interface {number} at {bus_id}: {error}");
                    claim_failed = true;
                    break;
                }
                claimed.push(number);
            }
            if claim_failed {
                continue;
            }
            info!("claimed interface(s) {claimed:?} at {bus_id}");
            // BootROM descriptor 3 only answers with wIndex=0. rusb's generic
            // string helper selects a language ID and therefore cannot read it.
            // Preserve the exact identity text NVIDIA tegrarcm expects without
            // touching the first bulk-IN greeting.
            let bootrom_identity = if let Some(identity) = &options.serial_number {
                info!("using supplied T234 BootROM identity at {bus_id}");
                identity.clone()
            } else {
                match read_bootrom_identity_string(&handle) {
                    Ok(identity) => identity,
                    Err(error) => {
                        warn!("cannot read T234 BootROM identity at {bus_id}: {error}");
                        continue;
                    }
                }
            };

            let mut usbip_devices = usbip::UsbIpServer::with_rusb_device_handles(vec![handle]);
            let Some(mut usbip_device) = usbip_devices.pop() else {
                warn!(
                    "cannot describe {:04x}:{:04x} at {bus_id}",
                    identity.vendor_id, identity.product_id
                );
                continue;
            };
            // Linux insists on resolving every advertised USB string before it
            // exposes the imported device, while tegrarcm explicitly fetches
            // descriptor index 3 even though T234 BootROM advertises iSerial=0.
            // Populate indices 1 and 2 first so the identity is always placed at
            // index 3, including recovery retries where the physical control
            // endpoint no longer answers any string-descriptor requests.
            usbip_device.set_manufacturer_name("NVIDIA Corp.");
            usbip_device.set_product_name("APX");
            usbip_device.set_serial_number(&bootrom_identity);
            info!("captured T234 BootROM identity descriptor at {bus_id}");

            info!(
                "exporting {:04x}:{:04x} as {bus_id}",
                identity.vendor_id, identity.product_id
            );
            server.add_device(usbip_device).await;
            exported.insert(bus_id, identity);
            reported_waiting = false;
        }

        let stale: Vec<String> = exported
            .keys()
            .filter(|bus_id| !present.contains(*bus_id))
            .cloned()
            .collect();
        for bus_id in stale {
            if server.remove_device(&bus_id).await.is_ok() {
                if let Some(identity) = exported.remove(&bus_id) {
                    info!(
                        "removed {:04x}:{:04x} at {bus_id}",
                        identity.vendor_id, identity.product_id
                    );
                }
            }
        }

        if exported.is_empty() && !reported_waiting {
            info!("waiting for a matching NVIDIA recovery device");
            reported_waiting = true;
        }

        tokio::select! {
            _ = tokio::signal::ctrl_c() => {
                info!("stopping USB/IP bridge");
                break;
            }
            _ = tokio::time::sleep(options.scan_interval) => {}
        }
    }

    server_task.abort();
    Ok(())
}
