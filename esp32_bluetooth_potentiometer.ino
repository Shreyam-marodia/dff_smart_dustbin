#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// These UUIDs perfectly match the Flutter app requirements[cite: 2]
#define SERVICE_UUID        "0000181d-0000-1000-8000-00805f9b34fb"
#define CHARACTERISTIC_UUID "00002a98-0000-1000-8000-00805f9b34fb"

BLEServer* pServer = NULL;
BLECharacteristic* pCharacteristic = NULL;
bool deviceConnected = false;
bool oldDeviceConnected = false;

// Potentiometer connected to GPIO 34
const int potPin = 34; 

// Callbacks to track when the phone connects and disconnects
class MyServerCallbacks: public BLEServerCallbacks {
    void onConnect(BLEServer* pServer) {
      deviceConnected = true;
    };
    void onDisconnect(BLEServer* pServer) {
      deviceConnected = false;
    }
};

void setup() {
  Serial.begin(115200);

  // Name the device. This is what shows up in the Flutter scan results!
  BLEDevice::init("ESP32_Scale");
  
  // Setup the BLE Server
  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new MyServerCallbacks());

  // Create the BLE Service[cite: 2]
  BLEService *pService = pServer->createService(SERVICE_UUID);

  // Create the BLE Characteristic with NOTIFY properties enabled[cite: 4]
  pCharacteristic = pService->createCharacteristic(
                      CHARACTERISTIC_UUID,
                      BLECharacteristic::PROPERTY_READ   |
                      BLECharacteristic::PROPERTY_NOTIFY
                    );

  // Required descriptor for notifications
  pCharacteristic->addDescriptor(new BLE2902());

  // Start the service
  pService->start();

  // Start advertising so the Flutter app can find it
  BLEAdvertising *pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->addServiceUUID(SERVICE_UUID);
  pAdvertising->setScanResponse(false);
  pAdvertising->setMinPreferred(0x0); 
  BLEDevice::startAdvertising();
  Serial.println("Waiting for a client connection to notify...");
}

void loop() {
  if (deviceConnected) {
    // Read the potentiometer (0 to 4095 on ESP32)
    int potValue = analogRead(potPin);
    
    // Simulate a weight between 0.0 and 100.0 kg
    float simulatedWeight = (potValue / 4095.0) * 100.0;
    
    // The Flutter app expects a string representation (e.g., "45.2")[cite: 4]
    String weightString = String(simulatedWeight, 1); 
    
    // Set the value and notify the phone
    pCharacteristic->setValue(weightString.c_str());
    pCharacteristic->notify();
    
    Serial.print("Notifying weight: ");
    Serial.println(weightString);
    
    // Send data every 1 second (adjust as needed)
    delay(1000); 
  }
  
  // Handle disconnection gracefully by restarting advertising
  if (!deviceConnected && oldDeviceConnected) {
      delay(500); // Give the bluetooth stack the chance to get things ready
      pServer->startAdvertising(); 
      Serial.println("Restarting advertising");
      oldDeviceConnected = deviceConnected;
  }
  
  // Handle connection
  if (deviceConnected && !oldDeviceConnected) {
      oldDeviceConnected = deviceConnected;
  }
}