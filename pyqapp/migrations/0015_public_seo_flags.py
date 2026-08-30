from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('pyqapp', '0014_alter_userpyqsubmission_display_name')]

    operations = [
        migrations.AddField(
            model_name='paper', name='is_public',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='importantquestionentry', name='is_public',
            field=models.BooleanField(default=False),
        ),
    ]
