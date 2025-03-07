# Home Assistant - enelgrid Integration

This custom integration for Home Assistant retrieves and imports **hourly and daily energy consumption data** from **Enel Italia**, making it available in Home Assistant's **Energy Dashboard**.

## 📋 Features

- 📊 Automatic login using Enel's SAML authentication process.
- 🕒 Fetches and tracks hourly energy consumption.
- 📈 Tracks monthly cumulative consumption.
- ✅ Seamless integration with Home Assistant Energy Dashboard.
- 🔁 Automatically updates data daily.

## 🛠️ Installation

### Manual Installation

1. Copy the **`enelgrid`** folder into:
    ```
    config/custom_components/enelgrid
    ```
2. Restart Home Assistant.
3. In Home Assistant, go to:  
   **Settings → Devices & Services → Add Integration**  
   Search for **"enelgrid"**.
4. Enter your Enel login credentials and POD details.

### Installation via HACS (Recommended)

1. In **HACS**, go to **Integrations**.
2. Add this repository as a **Custom Repository** (if it's not already in HACS).
3. Search for **"enelgrid"** and install.
4. Restart Home Assistant.
5. Follow the setup steps in **Settings → Devices & Services**.

## ⚙️ Configuration

During setup, you’ll need to provide:

- **Username** (your Enel account email).
- **Password**.
- **POD Number** (found on your Enel bill).
- **User Number** (also found on your Enel bill).

These credentials are stored securely in Home Assistant's `config_entries` storage.

## 🕒 Automatic Data Fetching

- Data is automatically fetched every day.
- Data is also fetched immediately upon first installation.

## 🏷️ Supported Features

| Feature                            | Status |
|------------------------------------|--------|
| Hourly Energy Data                 | ✅ |
| Daily Energy Data                  | ✅ |
| Monthly Cumulative Sensor          | ✅ |
| Energy Dashboard Integration       | ✅ |
| Automatic Login                    | ✅ |
| Automatic Daily Fetch              | ✅ |

## 🔗 Links

- 📖 [Enel Portal](https://www.enel.it/)
- 📘 [Home Assistant Docs](https://www.home-assistant.io/integrations/)

## 🧑‍💻 Developer

This integration was developed by [Sathia Francesco Musso](https://github.com/sathia-musso/enelgrid/).  
Contributions and feedback are welcome!

---

## 📜 License

This project is licensed under the MIT License.