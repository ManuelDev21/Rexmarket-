from django.core.management.base import BaseCommand
from usuariosApp.models import Sede, Perfil, Producto


class Command(BaseCommand):
    help = 'Carga las 28 sedes presenciales de INACAP en Chile y asigna Talca a los registros actuales.'

    def handle(self, *args, **kwargs):
        sedes_data = [
            ('INACAP Arica', 'XV', 'Arica', 'Av. Santa María 2190', -18.469315, -70.302702),
('INACAP Iquique', 'I', 'Iquique', 'Av. La Tirana 4310', -20.2720746209367, -70.12567484780965),
('INACAP Calama', 'II', 'Calama', 'Av. Granaderos 3250', -22.44765, -68.928473),
('INACAP Antofagasta', 'II', 'Antofagasta', 'Av. Edmundo Pérez Zujovic 11092', -23.560059, -70.400167),
('INACAP Copiapó', 'III', 'Copiapó', 'Yumbel 650', -27.360218, -70.334812),
('INACAP La Serena', 'IV', 'La Serena', 'Av. Francisco de Aguirre 389', -29.906569243056868, -71.26714353850795),
('INACAP Valparaíso', 'V', 'Valparaíso', 'Av. España 2250', -33.032942, -71.589339),
('INACAP Apoquindo', 'RM', 'Las Condes', 'Av. Apoquindo 7282', -33.407494, -70.552244),
('INACAP Maipú', 'RM', 'Maipú', 'Av. Américo Vespucio 974', -33.480429, -70.756011),
('INACAP Renca', 'RM', 'Renca', 'Bravo de Saravia 2980', -33.405696, -70.682912),
('INACAP Ñuñoa', 'RM', 'Ñuñoa', 'Brown Norte 290', -33.45293301109716, -70.59225549025386),
('INACAP Santiago Centro', 'RM', 'Santiago', 'Almirante Barroso 76', -33.44352, -70.662311),
('INACAP Santiago Sur', 'RM', 'Macul', 'Av. Vicuña Mackenna 3864', -33.490177, -70.616747),
('INACAP La Granja', 'RM', 'La Granja', 'Av. Américo Vespucio 315', -33.54334839144171, -70.62764545018419),
('INACAP Puente Alto', 'RM', 'Puente Alto', 'Av. Concha y Toro 2730', -33.585487, -70.581952),
('INACAP Rancagua', 'VI', 'Rancagua', 'Av. Nelson Pereira 2519', -34.135635, -70.739985),
('INACAP Curicó', 'VII', 'Curicó', 'Archipiélago Juan Fernández 2010', -34.979048, -71.211056),
('INACAP Talca', 'VII', 'Talca', 'Av. San Miguel 3496', -35.437164, -71.624041),
('INACAP Chillán', 'XVI', 'Chillán', 'Av. Bernardo O\'Higgins 441', -36.59397060037019, -72.10370140578786),
('INACAP Concepción-Talcahuano', 'VIII', 'Talcahuano', 'Autopista Concepción-Talcahuano 7421', -36.78214479508001, -73.07465523110272),
('INACAP San Pedro de la Paz', 'VIII', 'San Pedro de la Paz', 'Calle Los Mañíos 3925', -36.851738, -73.131078),
('INACAP Los Ángeles', 'VIII', 'Los Ángeles', 'Av. Ricardo Vicuña 825', -37.47171889529909, -72.35813881910421),
('INACAP Temuco', 'IX', 'Temuco', 'Luis Durand 02150', -38.72552630504243, -72.6322216707022),
('INACAP Valdivia', 'XIV', 'Valdivia', 'Av. Pedro Aguirre Cerda 2115', -39.80512210496295, -73.21501220028723   ),
('INACAP Osorno', 'X', 'Osorno', 'Av. René Soriano 2382', -40.580997822860176, -73.11137379921897),
('INACAP Puerto Montt', 'X', 'Puerto Montt', 'Av. Padre Harter 125', -41.465073, -72.939269),
('INACAP Coyhaique', 'XI', 'Coyhaique', 'Las Violetas 171', -45.58343, -72.0768),
('INACAP Punta Arenas', 'XII', 'Punta Arenas', 'Av. Bulnes Km 4 Norte', -53.130964, -70.879405),
        ]

        creadas = 0
        for nombre, region, comuna, direccion, lat, lng in sedes_data:
            _, created = Sede.objects.update_or_create(
                nombre=nombre,
                defaults={
                    'region': region,
                    'comuna': comuna,
                    'direccion': direccion,
                    'latitud': lat,
                    'longitud': lng,
                    'radio_permitido_metros': 150,
                    'activa': True,
                }
            )
            if created:
                creadas += 1

        # Asignar INACAP Talca por defecto a los registros antiguos sin sede
        sede_talca = Sede.objects.filter(nombre='INACAP Talca').first()
        if sede_talca:
            perfiles_actualizados = Perfil.objects.filter(sede__isnull=True).update(sede=sede_talca)
            productos_actualizados = Producto.objects.filter(sede__isnull=True).update(sede=sede_talca)
            self.stdout.write(self.style.SUCCESS(
                f'Listo: {creadas} sedes nuevas creadas (28 en total). '
                f'Se asignó INACAP Talca a {perfiles_actualizados} perfiles y {productos_actualizados} productos existentes.'
            ))