import json
import time
import urllib.parse
import urllib.request
from django.core.management.base import BaseCommand
from usuariosApp.models import Sede


class Command(BaseCommand):
    help = 'Completa la geocodificación de las 7 sedes restantes usando ArcGIS y Photon.'

    SEDES_PENDIENTES = [
        'INACAP Los Ángeles',
        'INACAP Temuco',
        'INACAP Valdivia',
        'INACAP Osorno',
        'INACAP Puerto Montt',
        'INACAP Coyhaique',
        'INACAP Punta Arenas',
    ]

    def buscar_arcgis(self, query):
        url = (
            "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?"
            + urllib.parse.urlencode({
                'SingleLine': query,
                'countryCode': 'CHL',
                'f': 'json',
                'maxLocations': 1,
            })
        )
        req = urllib.request.Request(url, headers={'User-Agent': 'RexMarket-INACAP/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            candidates = data.get('candidates', [])
            if candidates and candidates[0].get('score', 0) >= 80:
                loc = candidates[0]['location']
                return round(float(loc['y']), 6), round(float(loc['x']), 6), f"ArcGIS: {candidates[0].get('address')}"
        return None, None, None

    def buscar_photon(self, query):
        url = "https://photon.komoot.io/api/?" + urllib.parse.urlencode({'q': query, 'limit': 1})
        req = urllib.request.Request(url, headers={'User-Agent': 'RexMarket-INACAP/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            features = data.get('features', [])
            if features:
                coords = features[0]['geometry']['coordinates']
                return round(float(coords[1]), 6), round(float(coords[0]), 6), f"Photon: {query}"
        return None, None, None

    def buscar_coordenadas(self, consultas):
        for query in consultas:
            try:
                lat, lng, fuente = self.buscar_arcgis(query)
                if lat and lng:
                    return lat, lng, fuente
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  Error ArcGIS en '{query}': {e}"))

            try:
                lat, lng, fuente = self.buscar_photon(query)
                if lat and lng:
                    return lat, lng, fuente
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  Error Photon en '{query}': {e}"))

            time.sleep(0.5)
        return None, None, None

    def handle(self, *args, **kwargs):
        sedes = Sede.objects.filter(nombre__in=self.SEDES_PENDIENTES).order_by('id')
        actualizadas = 0
        revisar = []

        for sede in sedes:
            consultas = [
                f"{sede.nombre}, {sede.comuna}, Chile",
                f"{sede.direccion}, {sede.comuna}, Chile",
            ]

            lat, lng, fuente = self.buscar_coordenadas(consultas)

            if lat and lng:
                sede.latitud = lat
                sede.longitud = lng
                sede.save(update_fields=['latitud', 'longitud'])
                actualizadas += 1
                self.stdout.write(self.style.SUCCESS(
                    f"[OK] {sede.nombre} -> ({lat}, {lng}) | {fuente}"
                ))
            else:
                revisar.append(sede)
                self.stdout.write(self.style.ERROR(
                    f"[NO ENCONTRADA] {sede.nombre} ({sede.direccion}, {sede.comuna})"
                ))

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write(self.style.SUCCESS(f"Listo: {actualizadas}/7 sedes restantes actualizadas."))

        if revisar:
            self.stdout.write(self.style.WARNING("\nSedes para ajustar manualmente en /admin/:"))
            for s in revisar:
                self.stdout.write(f" - {s.nombre}: {s.direccion}, {s.comuna}")

        self.stdout.write("\n--- COPIA Y PEGA TODO ESTE BLOQUE EN sedes_data DE cargar_sedes.py ---")
        for s in Sede.objects.all().order_by('id'):
            dir_escapada = (s.direccion or '').replace("'", "\\'")
            self.stdout.write(
                f"('{s.nombre}', '{s.region}', '{s.comuna}', '{dir_escapada}', {s.latitud}, {s.longitud}),"
            )